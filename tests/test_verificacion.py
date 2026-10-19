"""Verificacion humana con Turnstile (B3, ADR 0019).

Lo que importa:

- Apagada (sin llave), no cambia nada.
- Encendida, nadie empieza un apartado sin un token que Cloudflare
  apruebe: ni sin token, ni con uno rechazado, vencido o ya usado.
- Una vez aprobado, ese apartado sigue sin volver a pedirla.
- Si Cloudflare falla o rechaza nuestra llave, la venta no se cae.
- La configuracion no deja encender media verificacion ni usar llaves de
  prueba en produccion.
"""

import json
import uuid
from urllib.parse import parse_qs

import httpx
import pytest

from app.core.config import settings
from app.services import limites
from app.services.verificacion import (
    LARGO_MAXIMO,
    URL_DE_CLOUDFLARE,
    Verificador,
    get_verificador,
)
from tests.test_catalog import API
from tests.test_checkout import escenario

pytestmark = pytest.mark.asyncio

SECRETA = "0x4AAAAAAA-secreta-de-pruebas"
SITE_KEY = "0x4AAAAAAA-publica-de-pruebas"
BUENO = "token-de-una-persona"


class Cloudflare:
    """Cloudflare simulado: anota lo que recibe y responde lo que se le pida."""

    def __init__(self) -> None:
        self.recibido: list[dict] = []
        self.respuesta: dict | Exception = {"success": True}
        self.status = 200

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert str(request.url) == URL_DE_CLOUDFLARE
        self.recibido.append(
            {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        )
        if isinstance(self.respuesta, Exception):
            raise self.respuesta
        return httpx.Response(self.status, json=self.respuesta)


@pytest.fixture
def cloudflare():
    return Cloudflare()


@pytest.fixture
def api(cliente_http, hold_service, redis_test, cloudflare, monkeypatch):
    from app.api.v1.apartados import get_hold_service
    from app.main import app

    monkeypatch.setattr(settings, "TURNSTILE_SECRET_KEY", SECRETA)
    monkeypatch.setattr(settings, "TURNSTILE_SITE_KEY", SITE_KEY)
    verificador = Verificador(httpx.MockTransport(cloudflare))
    app.dependency_overrides[get_hold_service] = lambda: hold_service
    app.dependency_overrides[limites.get_redis_limites] = lambda: redis_test
    app.dependency_overrides[get_verificador] = lambda: verificador
    return cliente_http


@pytest.fixture
async def esc(db, clientes):
    e = await escenario(db, clientes[0].id, por_zona=5)
    return e["performance_id"], e["seat_ids"]


async def apartar(api, pid, seat, hold=None, token=None):
    hold = hold or uuid.uuid4()
    headers = {"X-Palco-Verificacion": token} if token is not None else {}
    r = await api.post(
        f"{API}/performances/{pid}/apartados",
        json={"hold_id": str(hold), "seat_ids": [seat]},
        headers=headers,
    )
    return hold, r


def es_verificacion_requerida(r) -> bool:
    error = r.json()["error"]
    return (
        r.status_code == 403
        and error["codigo"] == "verificacion_requerida"
        and "persona" in error["mensaje"]
    )


# --------------------------------------------------------------------------
# Apagada y encendida
# --------------------------------------------------------------------------


async def test_apagada_no_pide_nada(api, esc, cloudflare, monkeypatch):
    monkeypatch.setattr(settings, "TURNSTILE_SECRET_KEY", "")
    pid, seats = esc

    _, r = await apartar(api, pid, seats[0])

    assert r.status_code == 201, r.text
    assert cloudflare.recibido == []


async def test_sin_token_no_se_aparta(api, esc, cloudflare):
    pid, seats = esc

    _, r = await apartar(api, pid, seats[0])

    assert es_verificacion_requerida(r), r.text
    assert cloudflare.recibido == []  # ni se le pregunta a Cloudflare


async def test_con_token_aprobado_se_aparta(api, esc, cloudflare):
    pid, seats = esc

    _, r = await apartar(api, pid, seats[0], token=BUENO)

    assert r.status_code == 201, r.text
    assert cloudflare.recibido == [
        {"secret": SECRETA, "response": BUENO, "remoteip": "127.0.0.1"}
    ]


@pytest.mark.parametrize(
    "codigos", [["invalid-input-response"], ["timeout-or-duplicate"], []]
)
async def test_token_rechazado_no_aparta(api, esc, cloudflare, codigos):
    pid, seats = esc
    cloudflare.respuesta = {"success": False, "error-codes": codigos}

    _, r = await apartar(api, pid, seats[0], token="token-de-un-bot")

    assert es_verificacion_requerida(r), r.text


async def test_rechazado_no_deja_butacas_apartadas(api, esc, cloudflare):
    pid, seats = esc
    cloudflare.respuesta = {"success": False, "error-codes": []}
    await apartar(api, pid, seats[0], token="token-de-un-bot")

    cloudflare.respuesta = {"success": True}
    _, r = await apartar(api, pid, seats[0], token=BUENO)

    assert r.status_code == 201, r.text


async def test_token_demasiado_largo_ni_se_consulta(api, esc, cloudflare):
    pid, seats = esc

    _, r = await apartar(api, pid, seats[0], token="x" * (LARGO_MAXIMO + 1))

    assert es_verificacion_requerida(r), r.text
    assert cloudflare.recibido == []


async def test_token_en_blanco_cuenta_como_sin_token(api, esc, cloudflare):
    pid, seats = esc

    _, r = await apartar(api, pid, seats[0], token="   ")

    assert es_verificacion_requerida(r), r.text
    assert cloudflare.recibido == []


# --------------------------------------------------------------------------
# Un apartado verificado sigue sin volver a pedirla
# --------------------------------------------------------------------------


async def test_el_mismo_apartado_sigue_sin_token(api, esc, cloudflare):
    pid, seats = esc
    hold, r = await apartar(api, pid, seats[0], token=BUENO)
    assert r.status_code == 201, r.text

    _, r = await apartar(api, pid, seats[1], hold=hold)

    assert r.status_code == 201, r.text
    assert len(cloudflare.recibido) == 1


async def test_otro_apartado_vuelve_a_pedirla(api, esc, cloudflare):
    pid, seats = esc
    await apartar(api, pid, seats[0], token=BUENO)

    _, r = await apartar(api, pid, seats[1])

    assert es_verificacion_requerida(r), r.text


async def test_la_marca_dura_lo_que_puede_vivir_el_apartado(api, esc, redis_test):
    pid, seats = esc
    hold, _ = await apartar(api, pid, seats[0], token=BUENO)

    ttl = await redis_test.ttl(f"humano:{hold.hex}")

    vida = settings.HOLD_TTL_SEGUNDOS + settings.HOLD_TTL_CHECKOUT_SEGUNDOS
    assert vida - 5 <= ttl <= vida


# --------------------------------------------------------------------------
# Si Cloudflare falla, la venta no se cae
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "falla",
    [
        httpx.ConnectError("sin red"),
        httpx.ReadTimeout("lento"),
        "error 500",
        "no es json",
        "llave mal puesta",
        "falla de ellos",
    ],
)
async def test_si_cloudflare_falla_se_deja_pasar(api, esc, cloudflare, falla, caplog):
    pid, seats = esc
    if isinstance(falla, Exception):
        cloudflare.respuesta = falla
    elif falla == "error 500":
        cloudflare.status = 500
    elif falla == "no es json":
        cloudflare.respuesta = None
    elif falla == "llave mal puesta":
        cloudflare.respuesta = {
            "success": False,
            "error-codes": ["invalid-input-secret"],
        }
    else:
        cloudflare.respuesta = {"success": False, "error-codes": ["internal-error"]}

    _, r = await apartar(api, pid, seats[0], token=BUENO)

    assert r.status_code == 201, r.text
    # Y PSD se entera (A4).
    assert any(
        reg.levelname == "ERROR" and "Turnstile" in reg.getMessage()
        for reg in caplog.records
    )


async def test_respuesta_que_no_es_json_no_truena():
    def responde(_):
        return httpx.Response(200, content=b"<html>mantenimiento</html>")

    verificador = Verificador(httpx.MockTransport(responde))

    resultado = await verificador.verificar(BUENO, "1.2.3.4")

    assert resultado.value == "sin_servicio"


async def test_el_mensaje_no_cuenta_detalles_tecnicos(api, esc, cloudflare):
    pid, seats = esc
    cloudflare.respuesta = {"success": False, "error-codes": ["timeout-or-duplicate"]}

    _, r = await apartar(api, pid, seats[0], token="viejo")

    texto = json.dumps(r.json(), ensure_ascii=False)
    for tecnico in ("Turnstile", "Cloudflare", "token", "timeout"):
        assert tecnico not in texto


# --------------------------------------------------------------------------
# Configuracion publica para el front
# --------------------------------------------------------------------------


async def test_el_front_lee_la_sitekey(api):
    r = await api.get(f"{API}/publico/configuracion")

    assert r.status_code == 200
    assert r.json() == {"verificacion_humana": {"activa": True, "site_key": SITE_KEY}}


async def test_apagada_el_front_no_muestra_el_widget(api, monkeypatch):
    monkeypatch.setattr(settings, "TURNSTILE_SITE_KEY", "")

    r = await api.get(f"{API}/publico/configuracion")

    assert r.json() == {"verificacion_humana": {"activa": False, "site_key": None}}


async def test_la_configuracion_no_lleva_la_secreta(api):
    r = await api.get(f"{API}/publico/configuracion")

    assert SECRETA not in r.text
