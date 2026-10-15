"""La pasarela real de Stripe, sin red.

Se sustituye solo el cliente HTTP: el SDK de Stripe arma de verdad cada
peticion, y aqui se revisa que lleve lo que importa (captura manual,
llave de idempotencia, monto en centavos) y que cada respuesta de
Stripe se traduzca al error correcto.
"""

import hashlib
import hmac
import json
import time

import pytest
import stripe

from app.services.pasarela import (
    CobroRechazado,
    FirmaInvalida,
    PasarelaNoDisponible,
    PasarelaSinConfigurar,
    StripePasarela,
)

SECRETO = "whsec_prueba_123"


class HTTPFalso(stripe.HTTPXClient):
    """Contesta lo que se le pida y anota cada peticion."""

    def __init__(self, respuestas: list[tuple[int, dict]]) -> None:
        super().__init__()
        self.respuestas = respuestas
        self.peticiones: list[tuple[str, str, dict, str | None]] = []

    async def request_with_retries_async(
        self,
        method,
        url,
        headers,
        post_data=None,
        max_network_retries=None,
        *,
        _usage=None,
    ):
        self.peticiones.append((method, url, dict(headers), post_data))
        estado, cuerpo = self.respuestas.pop(0)
        return json.dumps(cuerpo).encode(), estado, {"request-id": "req_x"}


def intento(estado: str = "requires_payment_method", **kw) -> dict:
    return {
        "id": "pi_123",
        "object": "payment_intent",
        "status": estado,
        "amount": 181000,
        "currency": "mxn",
        "client_secret": "pi_123_secret_abc",
        "metadata": {"orden_uuid": "u-1"},
        **kw,
    }


def error_de_stripe(mensaje: str = "No se puede") -> dict:
    return {"error": {"type": "invalid_request_error", "message": mensaje}}


def pasarela(respuestas) -> tuple[StripePasarela, HTTPFalso]:
    http = HTTPFalso(respuestas)
    return StripePasarela("sk_test_x", SECRETO, http_client=http), http


def firmar(payload: bytes, secreto: str = SECRETO) -> str:
    t = int(time.time())
    firma = hmac.new(
        secreto.encode(), f"{t}.".encode() + payload, hashlib.sha256
    ).hexdigest()
    return f"t={t},v1={firma}"


# --------------------------------------------------------------------------
# Peticiones
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_crear_intento_autoriza_sin_cobrar_y_es_idempotente():
    p, http = pasarela([(200, intento())])

    r = await p.crear_intento(
        monto_centavos=181000,
        moneda="MXN",
        descripcion="Palco PAL-1",
        metadata={"orden_uuid": "u-1"},
        idempotency_key="orden-u-1",
    )

    metodo, url, cabeceras, cuerpo = http.peticiones[0]
    assert (metodo, url) == ("post", "https://api.stripe.com/v1/payment_intents")
    assert cabeceras["Idempotency-Key"] == "orden-u-1"
    assert "capture_method=manual" in cuerpo
    assert "amount=181000" in cuerpo
    assert "currency=mxn" in cuerpo
    # Desde la API 2026-09-30 `payment_method_types` responde 400.
    assert "allowed_payment_method_types[0]=card" in cuerpo
    assert "&payment_method_types" not in cuerpo
    assert not cuerpo.startswith("payment_method_types")
    assert r.client_secret == "pi_123_secret_abc"
    assert r.moneda == "MXN"
    assert r.metadata == {"orden_uuid": "u-1"}


@pytest.mark.asyncio
async def test_capturar_lleva_su_llave_de_idempotencia():
    p, http = pasarela([(200, intento("succeeded"))])

    r = await p.capturar("pi_123", idempotency_key="capturar-u-1")

    metodo, url, cabeceras, _ = http.peticiones[0]
    assert metodo == "post"
    assert url.endswith("/v1/payment_intents/pi_123/capture")
    assert cabeceras["Idempotency-Key"] == "capturar-u-1"
    assert r.estado == "succeeded"


# --------------------------------------------------------------------------
# Errores: transitorio contra definitivo
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_captura_que_stripe_niega_es_rechazo_definitivo():
    p, _ = pasarela([(400, error_de_stripe()), (200, intento("canceled"))])

    with pytest.raises(CobroRechazado):
        await p.capturar("pi_123", idempotency_key="capturar-u-1")


@pytest.mark.asyncio
async def test_captura_que_ya_estaba_hecha_no_es_rechazo():
    """Un reintento de captura sobre algo ya cobrado no deshace la venta."""
    p, _ = pasarela([(400, error_de_stripe()), (200, intento("succeeded"))])

    r = await p.capturar("pi_123", idempotency_key="capturar-u-1")

    assert r.estado == "succeeded"


@pytest.mark.asyncio
async def test_stripe_caido_es_transitorio_nunca_rechazo():
    """Confundirlos cancelaria boletos de alguien que si pago."""
    # Si se tomara como rechazo, la consulta siguiente diria "autorizado"
    # y aun asi se cancelaria la venta.
    p, _ = pasarela(
        [
            (503, {"error": {"type": "api_error", "message": "x"}}),
            (200, intento("requires_capture")),
        ]
    )

    with pytest.raises(PasarelaNoDisponible):
        await p.capturar("pi_123", idempotency_key="capturar-u-1")


@pytest.mark.asyncio
async def test_lo_que_dice_stripe_queda_en_el_log(caplog):
    """Un 502 sin motivo en el log no se puede diagnosticar."""
    p, _ = pasarela([(400, error_de_stripe("parametro que ya no existe"))])

    with pytest.raises(PasarelaNoDisponible):
        await p.crear_intento(
            monto_centavos=1000,
            moneda="MXN",
            descripcion="x",
            metadata={},
            idempotency_key="k",
        )
    assert "parametro que ya no existe" in caplog.text
    assert "sk_test_x" not in caplog.text


@pytest.mark.asyncio
async def test_cancelar_algo_ya_cancelado_devuelve_su_estado():
    p, _ = pasarela([(400, error_de_stripe()), (200, intento("canceled"))])

    r = await p.cancelar("pi_123", idempotency_key="cancelar-u-1")

    assert r.estado == "canceled"


@pytest.mark.asyncio
async def test_sin_llave_secreta_no_intenta_cobrar():
    p = StripePasarela("", SECRETO)
    with pytest.raises(PasarelaSinConfigurar):
        await p.obtener("pi_123")


# --------------------------------------------------------------------------
# Firma del webhook
# --------------------------------------------------------------------------


def test_webhook_firmado_por_stripe_se_acepta():
    p = StripePasarela("sk_test_x", SECRETO)
    payload = json.dumps({"id": "evt_1", "type": "x"}).encode()

    assert p.verificar_evento(payload, firmar(payload))["id"] == "evt_1"


@pytest.mark.parametrize(
    "firma",
    [None, "", "t=1,v1=abc", "firma-inventada"],
)
def test_webhook_sin_firma_valida_se_rechaza(firma):
    p = StripePasarela("sk_test_x", SECRETO)
    payload = json.dumps({"id": "evt_1", "type": "x"}).encode()

    with pytest.raises(FirmaInvalida):
        p.verificar_evento(payload, firma)


def test_webhook_firmado_con_otro_secreto_se_rechaza():
    p = StripePasarela("sk_test_x", SECRETO)
    payload = json.dumps({"id": "evt_1", "type": "x"}).encode()

    with pytest.raises(FirmaInvalida):
        p.verificar_evento(payload, firmar(payload, "whsec_de_alguien_mas"))


def test_webhook_alterado_despues_de_firmar_se_rechaza():
    p = StripePasarela("sk_test_x", SECRETO)
    original = json.dumps({"id": "evt_1", "monto": 100}).encode()
    alterado = json.dumps({"id": "evt_1", "monto": 1}).encode()

    with pytest.raises(FirmaInvalida):
        p.verificar_evento(alterado, firmar(original))


def test_sin_secreto_de_webhook_no_se_acepta_nada():
    p = StripePasarela("sk_test_x", "")
    payload = b"{}"
    with pytest.raises(PasarelaSinConfigurar):
        p.verificar_evento(payload, firmar(payload))
