"""Cortesias (C1).

Lo que importa:

- Consumen el mismo inventario que la venta: lo que es cortesia no se
  vende, y lo vendido o apartado no se regala.
- Son boletos de verdad (con QR) y llegan con su propio correo.
- El barrido nunca intenta cobrarlas.
- Solo el organizador dueno del evento (o PSD) las da y las revoca.
- Revocar devuelve cada lugar a donde estaba, salvo si alguien ya entro.
"""

from datetime import timedelta

import pytest
from sqlalchemy import func, select, update

from app.core.errors import AsientoNoDisponible
from app.models.inventory import InventoryMove, PerformanceSeat
from app.models.outbox import OutboxEvent
from app.models.sales import Order, OrderItem, Ticket
from app.services.apartados import ApartadoService
from tests.test_apartados import fila, funcion_a_la_venta, nuevo_hold
from tests.test_barrido import barrido
from tests.test_catalog import API, auth, token_de
from tests.test_checkout import PasarelaFalsa
from tests.test_correo import CarteroFalso, bandeja

pytestmark = pytest.mark.asyncio


@pytest.fixture
def stripe_falso() -> PasarelaFalsa:
    return PasarelaFalsa()


@pytest.fixture
async def esc(db, clientes):
    """Funcion del organizador A: Luneta A-1..A-5 ($800) y Balcon ($400)."""
    return await funcion_a_la_venta(db, clientes[0].id, butacas_por_zona=5)


def de(usuarios, quien="org_a") -> dict:
    return auth(token_de(usuarios[quien]))


async def otorgar(cliente_http, usuarios, pid, seats, quien="org_a", **kw):
    cuerpo = {
        "seat_ids": seats,
        "email": "Prensa@Medio.MX",
        "nombre": "Reportera",
        "motivo": "Prensa",
        **kw,
    }
    return await cliente_http.post(
        f"{API}/performances/{pid}/cortesias", json=cuerpo, headers=de(usuarios, quien)
    )


async def orden_de(db, uuid) -> Order:
    return await db.scalar(
        select(Order)
        .where(Order.uuid == uuid)
        .execution_options(populate_existing=True)
    )


# --------------------------------------------------------------------------
# Otorgar
# --------------------------------------------------------------------------


async def test_otorgar_emite_boletos_con_qr_y_consume_inventario(
    cliente_http, db, esc, usuarios
):
    pid, seats = esc["performance_id"], esc["seat_ids"][:2]

    r = await otorgar(cliente_http, usuarios, pid, seats)

    assert r.status_code == 201, r.text
    cuerpo = r.json()
    assert cuerpo["estado"] == "pagada"
    assert cuerpo["email"] == "prensa@medio.mx"
    assert cuerpo["motivo"] == "Prensa"
    assert [x["asiento"] for x in cuerpo["lugares"]] == ["Fila A · 1", "Fila A · 2"]

    for s in seats:
        assert (await fila(db, pid, s)).estado == "cortesia"

    orden = await orden_de(db, cuerpo["uuid"])
    assert (orden.canal, orden.total, orden.subtotal, orden.iva) == (
        "cortesia",
        0,
        0,
        0,
    )
    assert orden.capturada_en is not None, "nada que cobrar"
    assert orden.stripe_payment_intent_id is None

    # Boletos de verdad: con el QR que la puerta acepta.
    boletos = (await cliente_http.get(f"{API}/ordenes/{cuerpo['uuid']}/boletos")).json()
    assert len(boletos["boletos"]) == 2
    assert all(b["llave_qr"] for b in boletos["boletos"]), "entran con su QR"

    movimientos = (
        await db.scalars(
            select(InventoryMove).where(InventoryMove.order_id == orden.id)
        )
    ).all()
    assert {(m.estado_previo, m.estado_nuevo, m.motivo) for m in movimientos} == {
        ("disponible", "cortesia", "cortesia")
    }
    assert all(m.actor_id == usuarios["org_a"].id for m in movimientos)

    tipos = set(
        (
            await db.scalars(
                select(OutboxEvent.tipo).where(OutboxEvent.agregado_uuid == orden.uuid)
            )
        ).all()
    )
    assert tipos == {"cortesia.emitida"}, "no es una venta: no avisa orden.pagada"


async def test_se_puede_dar_desde_un_lugar_bloqueado(cliente_http, db, esc, usuarios):
    pid, s = esc["performance_id"], esc["seat_ids"][0]
    await db.execute(
        update(PerformanceSeat)
        .where(PerformanceSeat.performance_id == pid, PerformanceSeat.seat_id == s)
        .values(estado="bloqueado")
    )

    r = await otorgar(cliente_http, usuarios, pid, [s])

    assert r.status_code == 201, r.text
    assert (await fila(db, pid, s)).estado == "cortesia"


async def test_antes_de_abrir_la_venta_tambien(cliente_http, db, esc, usuarios):
    from app.models.catalog import Performance

    await db.execute(
        update(Performance)
        .where(Performance.id == esc["performance_id"])
        .values(venta_abre_en=func.now() + timedelta(days=3))
    )

    r = await otorgar(
        cliente_http, usuarios, esc["performance_id"], esc["seat_ids"][:1]
    )

    assert r.status_code == 201, r.text


# --------------------------------------------------------------------------
# El mismo inventario
# --------------------------------------------------------------------------


@pytest.mark.parametrize("estado", ["vendido", "cortesia", "kill"])
async def test_no_se_regala_lo_que_ya_tiene_dueno(
    cliente_http, db, esc, usuarios, estado
):
    pid, s = esc["performance_id"], esc["seat_ids"]
    await db.execute(
        update(PerformanceSeat)
        .where(PerformanceSeat.performance_id == pid, PerformanceSeat.seat_id == s[0])
        .values(estado=estado)
    )

    r = await otorgar(cliente_http, usuarios, pid, [s[0], s[1]])

    assert r.status_code == 409
    assert r.json()["error"]["codigo"] == "cortesia_invalida"
    assert r.json()["error"]["detalle"]["seat_ids"] == [s[0]]
    assert (await fila(db, pid, s[1])).estado == "disponible", "todo o nada"


async def test_no_se_regala_lo_que_alguien_esta_comprando(
    cliente_http, db, esc, usuarios, hold_service
):
    pid, s = esc["performance_id"], esc["seat_ids"][0]
    await ApartadoService(db, hold_service).apartar(pid, [s], nuevo_hold())

    r = await otorgar(cliente_http, usuarios, pid, [s])

    assert r.status_code == 409
    assert (await fila(db, pid, s)).estado == "apartado"


async def test_un_apartado_vencido_si_se_puede_regalar(
    cliente_http, db, esc, usuarios, hold_service
):
    pid, s = esc["performance_id"], esc["seat_ids"][0]
    await ApartadoService(db, hold_service).apartar(pid, [s], nuevo_hold())
    await db.execute(
        update(PerformanceSeat)
        .where(PerformanceSeat.performance_id == pid, PerformanceSeat.seat_id == s)
        .values(hold_expira_en=func.now() - timedelta(minutes=5))
    )

    r = await otorgar(cliente_http, usuarios, pid, [s])

    assert r.status_code == 201, r.text


async def test_lo_que_es_cortesia_no_se_vende(
    cliente_http, db, esc, usuarios, hold_service, redis_test
):
    pid, s = esc["performance_id"], esc["seat_ids"][0]
    await otorgar(cliente_http, usuarios, pid, [s])

    with pytest.raises(AsientoNoDisponible):
        await ApartadoService(db, hold_service).apartar(pid, [s], nuevo_hold())


async def test_tope_por_cortesia(cliente_http, esc, usuarios):
    r = await otorgar(cliente_http, usuarios, esc["performance_id"], list(range(1, 22)))

    assert r.status_code == 422
    assert r.json()["error"]["codigo"] == "datos_invalidos"


# --------------------------------------------------------------------------
# Quien puede
# --------------------------------------------------------------------------


async def test_otro_organizador_no_ve_ni_da_cortesias(cliente_http, esc, usuarios):
    pid = esc["performance_id"]

    ajeno = await otorgar(cliente_http, usuarios, pid, esc["seat_ids"][:1], "org_b")
    inexistente = await otorgar(cliente_http, usuarios, pid + 10_000, [1], "org_b")
    lista = await cliente_http.get(
        f"{API}/performances/{pid}/cortesias", headers=de(usuarios, "org_b")
    )

    assert ajeno.status_code == inexistente.status_code == lista.status_code == 404
    assert ajeno.json() == inexistente.json()


async def test_psd_tambien_puede(cliente_http, esc, usuarios):
    r = await otorgar(
        cliente_http, usuarios, esc["performance_id"], esc["seat_ids"][:1], "admin"
    )

    assert r.status_code == 201, r.text


async def test_funcion_cancelada_no_admite_cortesias(cliente_http, db, esc, usuarios):
    from app.models.catalog import Performance

    await db.execute(
        update(Performance)
        .where(Performance.id == esc["performance_id"])
        .values(estado="cancelada")
    )

    r = await otorgar(
        cliente_http, usuarios, esc["performance_id"], esc["seat_ids"][:1]
    )

    assert r.status_code == 409
    assert "cancelada" in r.json()["error"]["mensaje"]


# --------------------------------------------------------------------------
# Revocar
# --------------------------------------------------------------------------


async def revocar(cliente_http, usuarios, uuid, quien="org_a"):
    return await cliente_http.post(
        f"{API}/cortesias/{uuid}/revocar", headers=de(usuarios, quien)
    )


async def test_revocar_devuelve_cada_lugar_a_donde_estaba(
    cliente_http, db, esc, usuarios
):
    pid, s = esc["performance_id"], esc["seat_ids"]
    await db.execute(
        update(PerformanceSeat)
        .where(PerformanceSeat.performance_id == pid, PerformanceSeat.seat_id == s[1])
        .values(estado="bloqueado")
    )
    uuid = (await otorgar(cliente_http, usuarios, pid, [s[0], s[1]])).json()["uuid"]

    r = await revocar(cliente_http, usuarios, uuid)

    assert r.status_code == 200, r.text
    assert r.json()["estado"] == "cancelada"
    assert r.json()["revocada_en"] is not None
    assert (await fila(db, pid, s[0])).estado == "disponible"
    assert (await fila(db, pid, s[1])).estado == "bloqueado"
    orden = await orden_de(db, uuid)
    boletos = (
        await db.scalars(
            select(Ticket.estado)
            .join(OrderItem, OrderItem.id == Ticket.order_item_id)
            .where(OrderItem.order_id == orden.id)
        )
    ).all()
    assert set(boletos) == {"cancelado"}
    # El lugar se puede volver a dar o vender.
    assert (await otorgar(cliente_http, usuarios, pid, [s[0]])).status_code == 201
    # Repetir la revocacion no hace nada.
    assert (await revocar(cliente_http, usuarios, uuid)).status_code == 200


async def test_no_se_revoca_si_alguien_ya_entro(cliente_http, db, esc, usuarios):
    pid, s = esc["performance_id"], esc["seat_ids"][0]
    uuid = (await otorgar(cliente_http, usuarios, pid, [s])).json()["uuid"]
    orden = await orden_de(db, uuid)
    await db.execute(
        update(Ticket)
        .where(
            Ticket.order_item_id.in_(
                select(OrderItem.id).where(OrderItem.order_id == orden.id)
            )
        )
        .values(estado="usado")
    )

    r = await revocar(cliente_http, usuarios, uuid)

    assert r.status_code == 409
    assert "ya entró" in r.json()["error"]["mensaje"]
    assert (await fila(db, pid, s)).estado == "cortesia"


async def test_otro_organizador_no_revoca(cliente_http, esc, usuarios):
    uuid = (
        await otorgar(
            cliente_http, usuarios, esc["performance_id"], esc["seat_ids"][:1]
        )
    ).json()["uuid"]

    r = await revocar(cliente_http, usuarios, uuid, "org_b")

    assert r.status_code == 404


async def test_listar_las_cortesias_de_la_funcion(cliente_http, esc, usuarios):
    pid, s = esc["performance_id"], esc["seat_ids"]
    primera = (await otorgar(cliente_http, usuarios, pid, [s[0]])).json()["uuid"]
    await otorgar(
        cliente_http, usuarios, pid, [s[1], s[2]], motivo="Patrocinador", nombre="Ana"
    )
    await revocar(cliente_http, usuarios, primera)

    r = await cliente_http.get(
        f"{API}/performances/{pid}/cortesias", headers=de(usuarios)
    )

    assert r.status_code == 200
    vistas = {c["motivo"]: c for c in r.json()}
    assert vistas["Patrocinador"]["estado"] == "pagada"
    assert len(vistas["Patrocinador"]["lugares"]) == 2
    assert vistas["Prensa"]["estado"] == "cancelada"


# --------------------------------------------------------------------------
# Barrido y correo
# --------------------------------------------------------------------------


async def test_el_barrido_nunca_intenta_cobrarla(
    cliente_http, db, esc, usuarios, hold_service, stripe_falso
):
    uuid = (
        await otorgar(
            cliente_http, usuarios, esc["performance_id"], esc["seat_ids"][:1]
        )
    ).json()["uuid"]
    # Una cortesia de hace dias: el barrido la veria si buscara capturas.
    await db.execute(
        update(Order)
        .where(Order.uuid == uuid)
        .values(pagada_en=func.now() - timedelta(days=6))
    )

    r = await barrido(db, hold_service, stripe_falso).una_vuelta()

    assert stripe_falso.capturas == []
    assert (r.capturas_hechas, r.capturas_atrasadas, r.errores) == (0, 0, 0)
    assert (await orden_de(db, uuid)).estado == "pagada"


async def test_llega_su_propio_correo(cliente_http, db, esc, usuarios):
    await otorgar(cliente_http, usuarios, esc["performance_id"], esc["seat_ids"][:2])
    cartero = CarteroFalso()

    r = await bandeja(db, cartero).una_vuelta()

    assert r.entregados == 1
    m = cartero.enviados[0]
    assert m.para == "prensa@medio.mx"
    assert m.asunto.startswith("¡Listo! Tus boletos de cortesía para ")
    assert m.imagenes == []
    for parte in (m.texto, m.html):
        assert "boletos de cortesía" in parte.lower()
        assert "Resumen de pago" not in parte
        assert "$" not in parte
        assert "Tu compra" not in parte


async def test_revocada_antes_del_correo_no_se_manda(cliente_http, db, esc, usuarios):
    uuid = (
        await otorgar(
            cliente_http, usuarios, esc["performance_id"], esc["seat_ids"][:1]
        )
    ).json()["uuid"]
    await revocar(cliente_http, usuarios, uuid)
    cartero = CarteroFalso()

    await bandeja(db, cartero).una_vuelta()

    assert cartero.enviados == []
