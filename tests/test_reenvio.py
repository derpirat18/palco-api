"""Reenviar la liga de los boletos (BOL15).

Lo que importa:

- El correo solo va al correo de la compra, y la respuesta lo dice tapado.
- Hay tope por compra: quien tenga la liga no llena de correos al comprador.
- Sin boletos que puedan entrar (reembolsada, sin pagar) no se reenvia.
- Sale por la bandeja, igual que el original.
"""

import uuid as _uuid

import pytest
from sqlalchemy import func, select, update

from app.models.outbox import OutboxEvent
from app.models.sales import Order
from app.services.reenvio import REENVIOS_POR_HORA, TIPO, tapar_correo
from tests.test_boletos import orden_pagada
from tests.test_checkout import PasarelaFalsa, orden_iniciada
from tests.test_correo import CarteroFalso, bandeja

pytestmark = pytest.mark.asyncio

API = "/api/v1/publico/ordenes"


@pytest.fixture
def stripe_falso() -> PasarelaFalsa:
    return PasarelaFalsa()


async def reenvios(db, orden_uuid) -> int:
    return await db.scalar(
        select(func.count())
        .select_from(OutboxEvent)
        .where(OutboxEvent.agregado_uuid == orden_uuid, OutboxEvent.tipo == TIPO)
    )


@pytest.mark.parametrize(
    ("correo", "tapado"),
    [
        ("ana@correo.mx", "a•••@correo.mx"),
        ("juanma@gmail.com", "ju•••@gmail.com"),
        ("x@y.mx", "x•••@y.mx"),
        ("sin-arroba", "•••"),
    ],
)
async def test_tapar_correo(correo, tapado):
    assert tapar_correo(correo) == tapado


async def test_reenvia_al_correo_de_la_compra(
    cliente_http, db, clientes, hold_service, stripe_falso
):
    orden = await orden_pagada(db, hold_service, stripe_falso, clientes[0].id)
    cartero = CarteroFalso()
    await bandeja(db, cartero).una_vuelta()  # el correo original
    assert len(cartero.enviados) == 1

    r = await cliente_http.post(f"{API}/{orden.uuid}/reenviar")

    assert r.status_code == 200, r.text
    assert r.json() == {"enviado_a": "a•••@correo.mx"}
    hecho = await db.scalar(
        select(OutboxEvent).where(
            OutboxEvent.agregado_uuid == orden.uuid, OutboxEvent.tipo == TIPO
        )
    )
    assert "ana@correo.mx" not in str(hecho.payload), "sin datos personales"

    await bandeja(db, cartero).una_vuelta()
    assert [m.para for m in cartero.enviados] == ["ana@correo.mx"] * 2
    assert cartero.enviados[1].html == cartero.enviados[0].html


async def test_tope_por_compra(cliente_http, db, clientes, hold_service, stripe_falso):
    orden = await orden_pagada(db, hold_service, stripe_falso, clientes[0].id)
    for _ in range(REENVIOS_POR_HORA):
        r = await cliente_http.post(f"{API}/{orden.uuid}/reenviar")
        assert r.status_code == 200, r.text

    r = await cliente_http.post(f"{API}/{orden.uuid}/reenviar")

    assert r.status_code == 429
    assert r.json()["error"]["codigo"] == "demasiados_reenvios"
    assert await reenvios(db, orden.uuid) == REENVIOS_POR_HORA


async def test_sin_boletos_que_entren_no_se_reenvia(
    cliente_http, db, clientes, hold_service, stripe_falso
):
    _, _, _, iniciada = await orden_iniciada(
        db, hold_service, stripe_falso, clientes[0].id
    )
    r = await cliente_http.post(f"{API}/{iniciada.orden.uuid}/reenviar")
    assert r.status_code == 409
    assert r.json()["error"]["codigo"] == "sin_boletos_por_reenviar"

    orden = await orden_pagada(db, hold_service, stripe_falso, clientes[1].id)
    await db.execute(
        update(Order).where(Order.id == orden.id).values(estado="reembolsada")
    )
    r = await cliente_http.post(f"{API}/{orden.uuid}/reenviar")
    assert r.status_code == 409
    assert await reenvios(db, orden.uuid) == 0


async def test_compra_que_no_existe(cliente_http):
    r = await cliente_http.post(f"{API}/{_uuid.uuid4()}/reenviar")
    assert r.status_code == 404


async def test_si_se_reembolsa_antes_de_salir_no_se_manda(
    cliente_http, db, clientes, hold_service, stripe_falso
):
    orden = await orden_pagada(db, hold_service, stripe_falso, clientes[0].id)
    cartero = CarteroFalso()
    await bandeja(db, cartero).una_vuelta()
    r = await cliente_http.post(f"{API}/{orden.uuid}/reenviar")
    assert r.status_code == 200
    await db.execute(
        update(Order).where(Order.id == orden.id).values(estado="reembolsada")
    )

    resultado = await bandeja(db, cartero).una_vuelta()

    assert resultado.entregados == 1, "se marca atendido"
    assert len(cartero.enviados) == 1, "pero no sale otro correo"
