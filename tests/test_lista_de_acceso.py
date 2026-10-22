"""Lista de boletos para la puerta (E2).

Lo que importa: salen solo los boletos que pueden entrar (pagados,
validos o ya usados), en el orden en que se recorre la sala, y sin datos
de contacto. La ven PSD, el organizador dueno y su personal de acceso;
para cualquier otro, la funcion no existe.
"""

import pytest
from sqlalchemy import select, update

from app.models.sales import Order, OrderItem, Ticket
from tests.test_catalog import API, igual_que_inexistente
from tests.test_cortesias import de, esc, otorgar, revocar, stripe_falso  # noqa: F401

pytestmark = pytest.mark.asyncio


async def lista(cliente_http, usuarios, pid, quien="org_a"):
    return await cliente_http.get(
        f"{API}/performances/{pid}/lista-de-acceso", headers=de(usuarios, quien)
    )


async def test_campos_y_orden_de_la_sala(cliente_http, esc, usuarios):  # noqa: F811
    pid, seats = esc["performance_id"], esc["seat_ids"]
    # Al reves a proposito: la lista ordena por zona, fila y numero.
    assert (await otorgar(cliente_http, usuarios, pid, [seats[2]])).status_code == 201
    assert (
        await otorgar(
            cliente_http, usuarios, pid, [seats[0], seats[1]], email="otra@medio.mx"
        )
    ).status_code == 201

    r = await lista(cliente_http, usuarios, pid)

    assert r.status_code == 200, r.text
    cuerpo = r.json()
    assert set(cuerpo) == {
        "evento",
        "recinto",
        "ciudad",
        "zona_horaria",
        "inicia_en",
        "apertura_puertas",
        "total",
        "usados",
        "boletos",
    }
    assert set(cuerpo["boletos"][0]) == {
        "zona",
        "asiento",
        "codigo",
        "folio",
        "titular",
        "cortesia",
        "estado",
    }
    assert (cuerpo["total"], cuerpo["usados"]) == (3, 0)
    asientos = [b["asiento"] for b in cuerpo["boletos"]]
    assert asientos == sorted(asientos, key=lambda a: int(a.split("· ")[1]))
    assert all(b["cortesia"] and b["titular"] == "Reportera" for b in cuerpo["boletos"])
    # Nada de contacto en una hoja que se queda en la puerta.
    assert "@" not in r.text


async def test_solo_lo_que_puede_entrar(cliente_http, db, esc, usuarios):  # noqa: F811
    pid, seats = esc["performance_id"], esc["seat_ids"]
    entra = (await otorgar(cliente_http, usuarios, pid, [seats[0]])).json()
    ya_entro = (await otorgar(cliente_http, usuarios, pid, [seats[1]])).json()
    revocada = (await otorgar(cliente_http, usuarios, pid, [seats[2]])).json()
    assert (await revocar(cliente_http, usuarios, revocada["uuid"])).status_code == 200

    orden = await db.scalar(select(Order).where(Order.uuid == ya_entro["uuid"]))
    await db.execute(
        update(Ticket)
        .where(
            Ticket.order_item_id.in_(
                select(OrderItem.id).where(OrderItem.order_id == orden.id)
            )
        )
        .values(estado="usado")
    )
    await db.flush()

    cuerpo = (await lista(cliente_http, usuarios, pid)).json()

    folios = {b["folio"]: b["estado"] for b in cuerpo["boletos"]}
    assert folios == {entra["folio"]: "valido", ya_entro["folio"]: "usado"}
    assert (cuerpo["total"], cuerpo["usados"]) == (2, 1)


async def test_la_ven_psd_el_organizador_y_su_personal(cliente_http, esc, usuarios):  # noqa: F811
    pid = esc["performance_id"]
    for quien in ("admin", "org_a", "acceso"):
        assert (await lista(cliente_http, usuarios, pid, quien)).status_code == 200, (
            quien
        )


async def test_para_otro_organizador_no_existe(cliente_http, esc, usuarios):  # noqa: F811
    igual_que_inexistente(
        await lista(cliente_http, usuarios, esc["performance_id"], "org_b"),
        await lista(cliente_http, usuarios, 999999, "org_b"),
    )
    sin_sesion = await cliente_http.get(
        f"{API}/performances/{esc['performance_id']}/lista-de-acceso"
    )
    assert sin_sesion.status_code == 401
