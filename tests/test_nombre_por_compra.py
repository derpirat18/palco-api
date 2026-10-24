"""Nombre por compra.

Un correo es un solo comprador, y sus datos no se cambian si ya existia
(un correo ajeno no le cambia nada a su dueno). Pero cada compra guarda
el nombre que se escribio en ella, y la lista de la puerta y el correo de
los boletos usan ese: el que va a llegar a la puerta es el de esa compra.
"""

from uuid import UUID

import pytest
from sqlalchemy import select

from app.models.sales import Customer, Order
from app.services.confirmacion import armar_confirmacion
from tests.test_catalog import API
from tests.test_checkout import orden_iniciada
from tests.test_cortesias import de, esc, otorgar, stripe_falso  # noqa: F401

pytestmark = pytest.mark.asyncio


async def test_cada_compra_guarda_su_nombre_sin_cambiar_al_comprador(
    db,
    clientes,
    hold_service,
    stripe_falso,  # noqa: F811
):
    _, _, _, primera = await orden_iniciada(
        db, hold_service, stripe_falso, clientes[0].id, nombre="Ana", apellidos="López"
    )
    _, _, _, segunda = await orden_iniciada(
        db,
        hold_service,
        stripe_falso,
        clientes[0].id,
        nombre="  Beto ",
        apellidos="Ruiz",
    )

    uno = await db.get(Order, primera.orden.id)
    dos = await db.get(Order, segunda.orden.id)
    assert (uno.nombre_snap, uno.apellidos_snap) == ("Ana", "López")
    assert (dos.nombre_snap, dos.apellidos_snap) == ("Beto", "Ruiz")
    # Mismo correo, mismo comprador, y su nombre no cambia.
    assert uno.customer_id == dos.customer_id
    persona = await db.get(Customer, uno.customer_id)
    assert persona.nombre == "Ana"


async def test_la_puerta_y_el_correo_usan_el_nombre_de_esa_compra(
    cliente_http,
    db,
    esc,  # noqa: F811
    usuarios,
):
    pid, seats = esc["performance_id"], esc["seat_ids"]
    a = await otorgar(cliente_http, usuarios, pid, [seats[0]], nombre="Reportera")
    b = await otorgar(cliente_http, usuarios, pid, [seats[1]], nombre="Fotógrafo")
    assert a.status_code == b.status_code == 201

    r = await cliente_http.get(
        f"{API}/performances/{pid}/lista-de-acceso", headers=de(usuarios)
    )
    por_folio = {x["folio"]: x["titular"] for x in r.json()["boletos"]}
    assert por_folio == {a.json()["folio"]: "Reportera", b.json()["folio"]: "Fotógrafo"}

    # El correo (diseno de Nadia, BOL1) ya no saluda por nombre; el que
    # cuenta es el de la lista de la puerta, arriba.
    correo = await armar_confirmacion(db, UUID(b.json()["uuid"]), cortesia=True)
    assert correo is not None
    assert "Reportera" not in correo.texto, "nunca el nombre de otra compra"


async def test_orden_anterior_sin_nombre_usa_el_del_comprador(
    cliente_http,
    db,
    esc,  # noqa: F811
    usuarios,
):
    """Las ordenes de antes de la columna no tienen nombre guardado."""
    pid, seats = esc["performance_id"], esc["seat_ids"]
    creada = (await otorgar(cliente_http, usuarios, pid, [seats[0]])).json()
    orden = await db.scalar(select(Order).where(Order.uuid == UUID(creada["uuid"])))
    orden.nombre_snap = None
    await db.flush()

    r = await cliente_http.get(
        f"{API}/performances/{pid}/lista-de-acceso", headers=de(usuarios)
    )
    assert r.json()["boletos"][0]["titular"] == "Reportera"
