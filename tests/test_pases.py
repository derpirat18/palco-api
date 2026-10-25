"""Pasar un boleto a un acompanante (BOL6).

Lo que importa:

- Al pasarlo, el boleto cambia de portador y de QR: el QR de quien compro
  deja de entrar al instante, y el del acompanante entra con su nombre.
- El acompanante ve solo lo que le pasaron, con su propia liga; no puede
  volver a pasarlo.
- Quien compro lo recupera mientras no haya entrado; la liga del
  acompanante deja de mostrarlo y se le avisa.
- Todo esto, solo hasta que abre el escaneo (4 h antes).
- Al acompanante no le llega cuanto se pago.
"""

import uuid as _uuid

import pytest
from sqlalchemy import select

from app.models.outbox import OutboxEvent
from app.models.sales import Ticket, Transfer
from app.services import pases
from tests.test_boletos import boletos_en_base, orden_pagada, qr_de
from tests.test_checkout import PasarelaFalsa
from tests.test_correo import CarteroFalso, bandeja
from tests.test_cortesias import esc, otorgar  # noqa: F401
from tests.test_escaner import abrir_puerta, asignar, boletos_de, leer

pytestmark = pytest.mark.asyncio

PUB = "/api/v1/publico"
BETO = {"nombre": "Beto", "apellidos": "Ruiz Soto", "email": "Beto@Correo.MX"}


@pytest.fixture
def stripe_falso() -> PasarelaFalsa:
    return PasarelaFalsa()


@pytest.fixture
async def compra(db, clientes, hold_service, stripe_falso):
    orden = await orden_pagada(db, hold_service, stripe_falso, clientes[0].id)
    await bandeja(db, CarteroFalso()).una_vuelta()  # el correo de la compra
    return orden


async def pasar(cliente_http, orden_uuid, boleto_uuid, **datos):
    return await cliente_http.post(
        f"{PUB}/ordenes/{orden_uuid}/boletos/{boleto_uuid}/pasar",
        json={**BETO, **datos},
    )


async def recuperar(cliente_http, orden_uuid, boleto_uuid):
    return await cliente_http.post(
        f"{PUB}/ordenes/{orden_uuid}/boletos/{boleto_uuid}/recuperar"
    )


async def tus_boletos(cliente_http, orden_uuid) -> list[dict]:
    r = await cliente_http.get(f"/api/v1/ordenes/{orden_uuid}/boletos")
    assert r.status_code == 200, r.text
    return r.json()["boletos"]


async def releer(db, ident) -> Ticket:
    return await db.get(Ticket, ident, populate_existing=True)


async def pase_de(db, ticket_id) -> Transfer:
    return await db.scalar(
        select(Transfer)
        .where(Transfer.ticket_id == ticket_id)
        .order_by(Transfer.id.desc())
        .limit(1)
        .execution_options(populate_existing=True)
    )


# --------------------------------------------------------------------------
# Pasar
# --------------------------------------------------------------------------


async def test_pasarlo_cambia_de_portador_y_de_qr(cliente_http, db, compra):
    antes = await tus_boletos(cliente_http, compra.uuid)
    assert all(b["se_puede_pasar"] for b in antes)
    assert not any(b["se_puede_recuperar"] for b in antes)
    b0, _ = await boletos_en_base(db, compra.id)
    semilla = b0.semilla

    r = await pasar(cliente_http, compra.uuid, b0.uuid)

    assert r.status_code == 200, r.text
    uno, dos = r.json()["boletos"]
    assert uno["llave_qr"] is None, "el QR ya no es de quien compro"
    assert uno["pasado_a"] == "Beto R."
    assert (uno["se_puede_pasar"], uno["se_puede_recuperar"]) == (False, True)
    assert dos == antes[1], "el otro boleto no cambia"
    b0 = await releer(db, b0.id)
    assert b0.semilla != semilla
    assert b0.holder_customer_id != compra.customer_id
    pase = await pase_de(db, b0.id)
    assert (pase.estado, pase.para_email) == ("aceptada", "beto@correo.mx")


async def test_al_acompanante_le_llega_su_liga_sin_el_pago(
    cliente_http, db, compra, monkeypatch
):
    from app.core.config import settings

    monkeypatch.setattr(settings, "FRONT_URL", "https://palco.mx")
    b0, _ = await boletos_en_base(db, compra.id)
    await pasar(cliente_http, compra.uuid, b0.uuid)
    cartero = CarteroFalso()

    await bandeja(db, cartero).una_vuelta()

    (m,) = cartero.enviados
    pase = await pase_de(db, b0.id)
    assert m.para == "beto@correo.mx"
    assert m.asunto.startswith("Ana te pasó un boleto para ")
    for parte in (m.html, m.texto):
        assert f"https://palco.mx/pase/{pase.token}" in parte
        assert f"/orden/{compra.uuid}" not in parte, "nunca la liga de la compra"
        assert "$" not in parte, "al acompanante no le toca saber cuanto se pago"
        assert "Ver mi boleto" in parte
        assert b0.codigo not in parte
    hecho = await db.scalar(
        select(OutboxEvent).where(OutboxEvent.tipo == "boleto.pasado")
    )
    assert "beto" not in str(hecho.payload).lower(), "sin datos personales"


async def test_el_acompanante_ve_solo_lo_suyo(cliente_http, db, compra):
    b0, _ = await boletos_en_base(db, compra.id)
    await pasar(cliente_http, compra.uuid, b0.uuid)
    pase = await pase_de(db, b0.id)

    r = await cliente_http.get(f"{PUB}/pases/{pase.token}")

    assert r.status_code == 200, r.text
    (suyo,) = r.json()["boletos"]
    assert suyo["uuid"] == str(b0.uuid)
    assert suyo["llave_qr"]
    assert (suyo["se_puede_pasar"], suyo["se_puede_recuperar"]) == (False, False)
    assert r.json()["folio"] == compra.folio

    r = await cliente_http.get(f"{PUB}/pases/{'x' * 43}")
    assert r.status_code == 404


async def test_el_qr_viejo_ya_no_entra_y_el_nuevo_entra_a_su_nombre(
    cliente_http,
    db,
    esc,  # noqa: F811
    usuarios,
):
    pid = esc["performance_id"]
    orden = (
        await otorgar(cliente_http, usuarios, pid, esc["seat_ids"][:2], nombre="Lupe")
    ).json()
    assert (await asignar(cliente_http, usuarios, [esc["event_id"]])).status_code == 200
    b0 = (await boletos_de(db, orden["uuid"]))[0]
    qr_viejo = qr_de(b0)

    r = await pasar(cliente_http, orden["uuid"], b0.uuid)
    assert r.status_code == 200, r.text
    await abrir_puerta(db, pid)

    viejo = (await leer(cliente_http, usuarios, pid, qr_viejo)).json()
    assert viejo["resultado"] == "invalido"
    nuevo = (
        await leer(cliente_http, usuarios, pid, qr_de(await releer(db, b0.id)))
    ).json()
    assert nuevo["resultado"] == "valido"
    assert nuevo["boleto"]["titular"] == "Beto Ruiz Soto"


# --------------------------------------------------------------------------
# Recuperar
# --------------------------------------------------------------------------


async def test_recuperarlo_cambia_el_qr_otra_vez_y_avisa(cliente_http, db, compra):
    b0, _ = await boletos_en_base(db, compra.id)
    llave_original = (await tus_boletos(cliente_http, compra.uuid))[0]["llave_qr"]
    await pasar(cliente_http, compra.uuid, b0.uuid)
    pase = await pase_de(db, b0.id)
    cartero = CarteroFalso()
    await bandeja(db, cartero).una_vuelta()

    r = await recuperar(cliente_http, compra.uuid, b0.uuid)

    assert r.status_code == 200, r.text
    uno = r.json()["boletos"][0]
    assert uno["llave_qr"] and uno["llave_qr"] != llave_original
    assert uno["pasado_a"] is None
    assert (uno["se_puede_pasar"], uno["se_puede_recuperar"]) == (True, False)
    assert (await releer(db, b0.id)).holder_customer_id == compra.customer_id
    assert (await pase_de(db, b0.id)).estado == "cancelada"
    # La liga del acompanante ya no lo muestra.
    assert (await cliente_http.get(f"{PUB}/pases/{pase.token}")).status_code == 404

    await bandeja(db, cartero).una_vuelta()
    aviso = cartero.enviados[-1]
    assert aviso.para == "beto@correo.mx"
    assert aviso.asunto.startswith("Ya no tienes el boleto para ")
    assert "Ana recuperó el boleto" in aviso.texto
    assert f"/pase/{pase.token}" not in aviso.html, "sin liga que ya no sirve"


async def test_si_lo_recupera_antes_de_salir_el_correo_no_se_manda(
    cliente_http, db, compra
):
    b0, _ = await boletos_en_base(db, compra.id)
    await pasar(cliente_http, compra.uuid, b0.uuid)
    await recuperar(cliente_http, compra.uuid, b0.uuid)
    cartero = CarteroFalso()

    await bandeja(db, cartero).una_vuelta()

    # Solo el aviso de que ya no lo tiene; nunca una liga que no sirve.
    assert [m.asunto.split(" ")[0] for m in cartero.enviados] == ["Ya"]


# --------------------------------------------------------------------------
# Reglas
# --------------------------------------------------------------------------


async def test_no_a_si_mismo_ni_dos_veces_ni_recuperar_lo_propio(
    cliente_http, db, compra
):
    b0, b1 = await boletos_en_base(db, compra.id)

    r = await pasar(cliente_http, compra.uuid, b0.uuid, email="ANA@correo.mx")
    assert r.status_code == 409
    assert "correo de esta compra" in r.json()["error"]["mensaje"]

    assert (await pasar(cliente_http, compra.uuid, b0.uuid)).status_code == 200
    r = await pasar(cliente_http, compra.uuid, b0.uuid, email="carla@correo.mx")
    assert r.status_code == 409
    assert "Beto R." in r.json()["error"]["mensaje"]

    r = await recuperar(cliente_http, compra.uuid, b1.uuid)
    assert r.status_code == 409
    assert r.json()["error"]["codigo"] == "no_se_puede_pasar"


async def test_boleto_ajeno_o_inexistente(
    cliente_http, db, clientes, hold_service, stripe_falso, compra
):
    otra = await orden_pagada(db, hold_service, stripe_falso, clientes[1].id)
    ajeno = (await boletos_en_base(db, otra.id))[0]

    assert (await pasar(cliente_http, compra.uuid, ajeno.uuid)).status_code == 404
    assert (await pasar(cliente_http, compra.uuid, _uuid.uuid4())).status_code == 404
    assert (await pasar(cliente_http, _uuid.uuid4(), ajeno.uuid)).status_code == 404


async def test_ya_abierto_el_escaneo_no_se_pasa_ni_se_recupera(
    cliente_http, db, compra
):
    b0, b1 = await boletos_en_base(db, compra.id)
    await pasar(cliente_http, compra.uuid, b1.uuid)
    await abrir_puerta(db, compra.performance_id)

    vista = await tus_boletos(cliente_http, compra.uuid)
    assert not any(b["se_puede_pasar"] or b["se_puede_recuperar"] for b in vista)
    r = await pasar(cliente_http, compra.uuid, b0.uuid, email="carla@correo.mx")
    assert r.status_code == 409
    assert "taquilla" in r.json()["error"]["mensaje"]
    assert (await recuperar(cliente_http, compra.uuid, b1.uuid)).status_code == 409


async def test_ya_entro_no_se_pasa(cliente_http, db, compra):
    b0, _ = await boletos_en_base(db, compra.id)
    b0.estado = "usado"
    await db.flush()

    r = await pasar(cliente_http, compra.uuid, b0.uuid)

    assert r.status_code == 409
    assert r.json()["error"]["mensaje"] == "Este boleto ya entró."


async def test_tope_de_pases_por_dia(cliente_http, db, compra, monkeypatch):
    monkeypatch.setattr(pases, "PASES_POR_ORDEN_AL_DIA", 2)
    b0, _ = await boletos_en_base(db, compra.id)
    assert (await pasar(cliente_http, compra.uuid, b0.uuid)).status_code == 200
    assert (await recuperar(cliente_http, compra.uuid, b0.uuid)).status_code == 200
    assert (await pasar(cliente_http, compra.uuid, b0.uuid)).status_code == 200
    assert (await recuperar(cliente_http, compra.uuid, b0.uuid)).status_code == 200

    r = await pasar(cliente_http, compra.uuid, b0.uuid)

    assert r.status_code == 429
    assert r.json()["error"]["codigo"] == "demasiados_pases"
