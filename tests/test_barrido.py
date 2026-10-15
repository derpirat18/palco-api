"""Barrido: lo que queda a medias cuando no hay trafico ni reintentos.

Mismo Stripe falso que el checkout. Lo que importa: nada se suelta
mientras Redis diga que es de alguien, nada se cierra sin cancelar antes
en Stripe, una captura pendiente se termina, y la que se atrasa se avisa
una sola vez.
"""

from contextlib import asynccontextmanager
from datetime import timedelta

import pytest
from sqlalchemy import func, select, update

from app.models.inventory import InventoryMove, PerformanceSeat
from app.models.outbox import OutboxEvent
from app.models.sales import Order, OrderItem, Ticket
from app.services.barrido import BarridoService
from app.services.checkout import CheckoutService
from app.services.holds import clave_butaca
from app.services.pasarela import CANCELADO, PasarelaNoDisponible
from tests.test_apartados import fila
from tests.test_checkout import (
    PasarelaFalsa,
    apartar,
    comprador,
    escenario,
    orden_iniciada,
    recargar,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture
def stripe_falso() -> PasarelaFalsa:
    return PasarelaFalsa()


def barrido(db, holds, stripe) -> BarridoService:
    """Todas las sesiones del barrido son la de la prueba."""

    @asynccontextmanager
    async def sesiones():
        yield db

    return BarridoService(sesiones, holds, stripe)


async def vencer_en_postgres(db, hold: str, hace=timedelta(minutes=5)) -> None:
    await db.execute(
        update(PerformanceSeat)
        .where(PerformanceSeat.hold_id == hold)
        .values(hold_expira_en=func.now() - hace)
    )


async def vencer_en_redis(redis, pid: int, seats: list[int]) -> None:
    for s in seats:
        await redis.delete(clave_butaca(pid, s))


async def vencer_orden(db, orden_id: int, hace=timedelta(minutes=5)) -> None:
    await db.execute(
        update(Order).where(Order.id == orden_id).values(expira_en=func.now() - hace)
    )


# --------------------------------------------------------------------------
# 1. Apartados vencidos
# --------------------------------------------------------------------------


async def test_suelta_el_apartado_que_ya_vencio_en_las_dos_capas(
    db, clientes, hold_service, stripe_falso, redis_test
):
    esc = await escenario(db, clientes[0].id)
    pid, seats = esc["performance_id"], esc["luneta"][:2]
    hold = await apartar(db, hold_service, esc, seats)
    await vencer_en_postgres(db, hold)
    await vencer_en_redis(redis_test, pid, seats)

    r = await barrido(db, hold_service, stripe_falso).una_vuelta()

    assert r.apartados_soltados == 2
    for s in seats:
        f = await fila(db, pid, s)
        assert (f.estado, f.hold_id, f.hold_expira_en) == ("disponible", None, None)
    motivos = (
        await db.scalars(
            select(InventoryMove.motivo).where(
                InventoryMove.hold_id == hold,
                InventoryMove.estado_nuevo == "disponible",
            )
        )
    ).all()
    assert motivos == ["apartado vencido", "apartado vencido"]


async def test_no_suelta_lo_que_redis_sigue_dando_por_suyo(
    db, clientes, hold_service, stripe_falso
):
    """Un checkout extendio en Redis y su transaccion se revirtio: Postgres
    dice vencido, pero el comprador sigue con su apartado."""
    esc = await escenario(db, clientes[0].id)
    pid, seats = esc["performance_id"], esc["luneta"][:2]
    hold = await apartar(db, hold_service, esc, seats)
    await vencer_en_postgres(db, hold)

    r = await barrido(db, hold_service, stripe_falso).una_vuelta()

    assert r.apartados_soltados == 0
    assert (await fila(db, pid, seats[0])).hold_id == hold


async def test_respeta_la_gracia(db, clientes, hold_service, stripe_falso, redis_test):
    esc = await escenario(db, clientes[0].id)
    pid, seats = esc["performance_id"], esc["luneta"][:2]
    hold = await apartar(db, hold_service, esc, seats)
    await vencer_en_postgres(db, hold, hace=timedelta(seconds=10))
    await vencer_en_redis(redis_test, pid, seats)

    r = await barrido(db, hold_service, stripe_falso).una_vuelta()

    assert r.apartados_soltados == 0


# --------------------------------------------------------------------------
# 2. Ordenes pendientes vencidas
# --------------------------------------------------------------------------


async def test_cierra_la_orden_vencida_cancelando_primero_en_stripe(
    db, clientes, hold_service, stripe_falso, redis_test
):
    esc, hold, _, iniciada = await orden_iniciada(
        db, hold_service, stripe_falso, clientes[0].id
    )
    pid, seats = esc["performance_id"], esc["luneta"][:2]
    await vencer_orden(db, iniciada.orden.id)
    await vencer_en_postgres(db, hold)
    await vencer_en_redis(redis_test, pid, seats)

    r = await barrido(db, hold_service, stripe_falso).una_vuelta()

    orden = await recargar(db, Order, iniciada.orden.id)
    assert (orden.estado, orden.cerrada_motivo) == ("expirada", "apartado_vencido")
    assert stripe_falso.intentos[orden.stripe_payment_intent_id].estado == CANCELADO
    vivas = await db.scalar(
        select(func.count(OrderItem.id)).where(
            OrderItem.order_id == orden.id, OrderItem.anulado.is_(False)
        )
    )
    assert vivas == 0
    assert (r.ordenes_cerradas, r.apartados_soltados) == (1, 2)
    assert (await fila(db, pid, seats[0])).estado == "disponible"


async def test_vencida_pero_autorizada_y_aun_suya_se_vende(
    db, clientes, hold_service, stripe_falso
):
    """Pago justo al limite: Redis todavia es suyo, se le vende."""
    _, _, _, iniciada = await orden_iniciada(
        db, hold_service, stripe_falso, clientes[0].id
    )
    stripe_falso.autorizar(iniciada.orden.stripe_payment_intent_id)
    await vencer_orden(db, iniciada.orden.id)

    r = await barrido(db, hold_service, stripe_falso).una_vuelta()

    orden = await recargar(db, Order, iniciada.orden.id)
    assert orden.estado == "pagada"
    assert orden.capturada_en is not None
    assert r.ordenes_vendidas_tarde == 1


async def test_vencida_autorizada_sin_butaca_suelta_la_autorizacion(
    db, clientes, hold_service, stripe_falso, redis_test
):
    esc, _, _, iniciada = await orden_iniciada(
        db, hold_service, stripe_falso, clientes[0].id
    )
    pi = iniciada.orden.stripe_payment_intent_id
    stripe_falso.autorizar(pi)
    await vencer_orden(db, iniciada.orden.id)
    await vencer_en_redis(redis_test, esc["performance_id"], esc["luneta"][:2])

    await barrido(db, hold_service, stripe_falso).una_vuelta()

    orden = await recargar(db, Order, iniciada.orden.id)
    assert orden.estado == "expirada"
    assert stripe_falso.cancelaciones == [pi]
    assert stripe_falso.capturas == []
    assert await db.scalar(select(func.count(Ticket.id))) == 0


async def test_pago_en_proceso_no_se_cierra_todavia(
    db, clientes, hold_service, stripe_falso
):
    """Cerrar sin poder cancelar en Stripe dejaria un cobro sin orden."""
    _, _, _, iniciada = await orden_iniciada(
        db, hold_service, stripe_falso, clientes[0].id
    )
    stripe_falso.poner(iniciada.orden.stripe_payment_intent_id, estado="processing")
    stripe_falso.cancelacion_bloqueada = True
    await vencer_orden(db, iniciada.orden.id)

    r = await barrido(db, hold_service, stripe_falso).una_vuelta()

    assert (await recargar(db, Order, iniciada.orden.id)).estado == "pendiente"
    assert r.ordenes_cerradas == 0


async def test_una_orden_con_error_no_detiene_a_las_demas(
    db, clientes, hold_service, stripe_falso
):
    esc = await escenario(db, clientes[0].id, por_zona=8)
    pid, luneta = esc["performance_id"], esc["luneta"]
    servicio = CheckoutService(db, hold_service, stripe_falso)
    h1 = await apartar(db, hold_service, esc, luneta[0:2])
    o1 = await servicio.iniciar(pid, h1, comprador(email="uno@correo.mx"))
    h2 = await apartar(db, hold_service, esc, luneta[4:6])
    o2 = await servicio.iniciar(pid, h2, comprador(email="dos@correo.mx"))
    for o in (o1, o2):
        await vencer_orden(db, o.orden.id)
    stripe_falso.falla_obtener_para = {o1.orden.stripe_payment_intent_id}

    r = await barrido(db, hold_service, stripe_falso).una_vuelta()

    assert (r.errores, r.ordenes_cerradas) == (1, 1)
    assert (await recargar(db, Order, o1.orden.id)).estado == "pendiente"
    assert (await recargar(db, Order, o2.orden.id)).estado == "expirada"


# --------------------------------------------------------------------------
# 3 y 4. Capturas pendientes y atrasadas
# --------------------------------------------------------------------------


async def pagada_sin_capturar(db, holds, stripe, cliente_id, hace) -> Order:
    _, _, servicio, iniciada = await orden_iniciada(db, holds, stripe, cliente_id)
    stripe.autorizar(iniciada.orden.stripe_payment_intent_id)
    stripe.falla_captura = PasarelaNoDisponible("Stripe no responde")
    with pytest.raises(PasarelaNoDisponible):
        await servicio.finalizar(iniciada.orden.id)
    await db.execute(
        update(Order)
        .where(Order.id == iniciada.orden.id)
        .values(pagada_en=func.now() - hace)
    )
    return await recargar(db, Order, iniciada.orden.id)


async def test_termina_la_captura_que_quedo_pendiente(
    db, clientes, hold_service, stripe_falso
):
    orden = await pagada_sin_capturar(
        db, hold_service, stripe_falso, clientes[0].id, timedelta(minutes=10)
    )
    stripe_falso.falla_captura = None

    r = await barrido(db, hold_service, stripe_falso).una_vuelta()

    assert (await recargar(db, Order, orden.id)).capturada_en is not None
    assert r.capturas_hechas == 1
    assert len(stripe_falso.capturas) == 1


async def test_no_se_cruza_con_una_captura_recien_hecha(
    db, clientes, hold_service, stripe_falso
):
    await pagada_sin_capturar(
        db, hold_service, stripe_falso, clientes[0].id, timedelta(seconds=5)
    )
    stripe_falso.falla_captura = None

    r = await barrido(db, hold_service, stripe_falso).una_vuelta()

    assert r.capturas_hechas == 0
    assert stripe_falso.capturas == []


async def test_captura_atrasada_se_avisa_una_sola_vez(
    db, clientes, hold_service, stripe_falso
):
    orden = await pagada_sin_capturar(
        db, hold_service, stripe_falso, clientes[0].id, timedelta(days=6)
    )
    servicio = barrido(db, hold_service, stripe_falso)

    primera = await servicio.una_vuelta()
    segunda = await servicio.una_vuelta()

    assert (primera.capturas_atrasadas, segunda.capturas_atrasadas) == (1, 0)
    assert primera.errores == 1, "el reintento de captura sigue fallando"
    avisos = (
        await db.scalars(
            select(OutboxEvent.payload).where(
                OutboxEvent.agregado_uuid == orden.uuid,
                OutboxEvent.tipo == "orden.captura_atrasada",
            )
        )
    ).all()
    assert len(avisos) == 1
    assert avisos[0]["folio"] == orden.folio


async def test_sin_pendientes_la_vuelta_no_hace_nada(
    db, clientes, hold_service, stripe_falso
):
    r = await barrido(db, hold_service, stripe_falso).una_vuelta()
    assert not r.hubo_algo()
