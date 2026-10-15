"""Checkout y cobro: de un apartado a boletos pagados.

Contra Postgres y Redis reales; solo Stripe es falso, y puede fallar a
voluntad. Lo que se prueba son las reglas que no se negocian:

- Nunca cobrar sin emitir ni emitir sin cobrar.
- Si la butaca se perdio, la autorizacion se cancela y no hay cargo.
- Un tropiezo de Stripe nunca cancela boletos de alguien que si pago.
- Webhook y front pueden llegar repetidos o al mismo tiempo: un solo
  juego de boletos, una sola captura.
"""

import asyncio
import dataclasses
import hashlib
import hmac
import json
import re
import time
import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select, update

from app.core.config import settings
from app.core.errors import (
    ApartadoExpirado,
    DejaButacaSuelta,
    FuncionNoALaVenta,
)
from app.core.telefonos import TelefonoInvalido
from app.models.base import EstadoAsiento, EstadoEvento
from app.models.catalog import Client, Event
from app.models.inventory import InventoryMove, PerformanceSeat
from app.models.outbox import OutboxEvent
from app.models.payments import PaymentEvent
from app.models.sales import Consent, Customer, Order, OrderItem, Ticket
from app.services.apartados import ApartadoService
from app.services.checkout import (
    CheckoutService,
    DatosDelComprador,
    IntentoNoCorresponde,
    OrdenSinCobro,
    PagoEnProceso,
)
from app.services.consentimientos import ConsentimientoInvalido
from app.services.holds import clave_butaca
from app.services.pasarela import (
    AUTORIZADO,
    CANCELADO,
    COBRADO,
    CobroRechazado,
    IntentoDePago,
    PasarelaNoDisponible,
    StripePasarela,
)
from tests.test_apartados import fila, funcion_a_la_venta, nuevo_hold, poner_estado

pytestmark = pytest.mark.asyncio

API = "/api/v1"
SECRETO = "whsec_pruebas_checkout"


# --------------------------------------------------------------------------
# Stripe falso
# --------------------------------------------------------------------------


class PasarelaFalsa:
    """Se comporta como Stripe en lo que importa aqui.

    Idempotente por llave como Stripe, y con interruptores para fallar.
    La firma del webhook la verifica la pasarela real.
    """

    def __init__(self) -> None:
        self.intentos: dict[str, IntentoDePago] = {}
        self._por_llave: dict[str, str] = {}
        self._llaves_de_captura: set[str] = set()
        self.capturas: list[str] = []
        self.cancelaciones: list[str] = []
        self.falla_captura: Exception | None = None
        self.falla_obtener: Exception | None = None
        self._real = StripePasarela("sk_test_x", SECRETO)

    async def crear_intento(
        self, *, monto_centavos, moneda, descripcion, metadata, idempotency_key
    ) -> IntentoDePago:
        if idempotency_key in self._por_llave:
            return self.intentos[self._por_llave[idempotency_key]]
        pid = f"pi_{len(self.intentos) + 1}"
        self.intentos[pid] = IntentoDePago(
            id=pid,
            estado="requires_payment_method",
            monto_centavos=monto_centavos,
            moneda=moneda,
            client_secret=f"{pid}_secret_x",
            metadata=dict(metadata),
        )
        self._por_llave[idempotency_key] = pid
        return self.intentos[pid]

    def poner(self, pid: str, **cambios) -> None:
        self.intentos[pid] = dataclasses.replace(self.intentos[pid], **cambios)

    def autorizar(self, pid: str) -> None:
        self.poner(pid, estado=AUTORIZADO)

    async def obtener(self, intento_id: str) -> IntentoDePago:
        if self.falla_obtener:
            raise self.falla_obtener
        return self.intentos[intento_id]

    async def capturar(self, intento_id: str, *, idempotency_key: str):
        if self.falla_captura:
            raise self.falla_captura
        if idempotency_key in self._llaves_de_captura:
            return self.intentos[intento_id]
        if self.intentos[intento_id].estado != AUTORIZADO:
            raise CobroRechazado("No se puede capturar.")
        self.poner(intento_id, estado=COBRADO)
        self._llaves_de_captura.add(idempotency_key)
        self.capturas.append(intento_id)
        return self.intentos[intento_id]

    async def cancelar(self, intento_id: str, *, idempotency_key: str):
        if self.intentos[intento_id].estado not in (COBRADO, CANCELADO):
            self.poner(intento_id, estado=CANCELADO)
            self.cancelaciones.append(intento_id)
        return self.intentos[intento_id]

    def verificar_evento(self, payload: bytes, firma: str | None) -> dict:
        return self._real.verificar_evento(payload, firma)


@pytest.fixture
def stripe_falso() -> PasarelaFalsa:
    return PasarelaFalsa()


def comprador(**kw) -> DatosDelComprador:
    datos = {
        "email": "Ana.Lopez@Correo.MX",
        "nombre": "Ana",
        "apellidos": "Lopez",
        "telefono": "55 1234 5678",
        "acepta_aviso": True,
        "aviso_version": "2026-10",
        "acepta_marketing": False,
    }
    datos.update(kw)
    return DatosDelComprador(**datos)


async def escenario(db, cliente_id: int, por_zona: int = 5) -> dict:
    """Funcion a la venta; luneta a $800 y balcon a $400, una fila cada una."""
    esc = await funcion_a_la_venta(db, cliente_id, butacas_por_zona=por_zona)
    esc["luneta"] = esc["seat_ids"][:por_zona]
    return esc


async def apartar(db, holds, esc, seats, hold=None) -> str:
    hold = hold or nuevo_hold()
    await ApartadoService(db, holds).apartar(esc["performance_id"], seats, hold)
    return hold


async def orden_iniciada(db, holds, stripe, cliente_id, n=2, **kw):
    esc = await escenario(db, cliente_id)
    hold = await apartar(db, holds, esc, esc["luneta"][:n])
    servicio = CheckoutService(db, holds, stripe)
    iniciada = await servicio.iniciar(esc["performance_id"], hold, comprador(**kw))
    return esc, hold, servicio, iniciada


async def recargar(db, modelo, ident):
    return await db.scalar(
        select(modelo)
        .where(modelo.id == ident)
        .execution_options(populate_existing=True)
    )


# --------------------------------------------------------------------------
# Iniciar
# --------------------------------------------------------------------------


async def test_la_orden_congela_importes_y_condiciones(
    db, clientes, hold_service, stripe_falso
):
    _, hold, _, iniciada = await orden_iniciada(
        db, hold_service, stripe_falso, clientes[0].id
    )
    orden = iniciada.orden

    # 2 x 800, 10 % de servicio por butaca, 50 por orden.
    assert orden.subtotal == Decimal("1600.00")
    assert orden.cargo_servicio == Decimal("160.00")
    assert orden.cargo_orden == Decimal("50.00")
    assert orden.total == Decimal("1810.00")
    assert orden.comision_pct_snap == Decimal("10.00")
    assert orden.cargo_servicio_pct_snap == Decimal("10.00")
    assert orden.cargo_orden_snap == Decimal("50.00")
    assert orden.estado == "pendiente"
    assert orden.hold_id == hold
    assert re.fullmatch(r"PAL-[0-9A-HJKMNP-TV-Z]{8}", orden.folio)

    assert [(p.zona_snap, p.asiento_snap) for p in iniciada.partidas] == [
        ("Luneta", "Fila A · 1"),
        ("Luneta", "Fila A · 2"),
    ]
    assert all(p.importe == Decimal("880.00") for p in iniciada.partidas)

    intento = stripe_falso.intentos[orden.stripe_payment_intent_id]
    assert intento.monto_centavos == 181000
    assert intento.moneda == "MXN"
    assert intento.metadata["orden_uuid"] == str(orden.uuid)
    assert iniciada.client_secret == intento.client_secret


async def test_cambiar_la_comision_despues_no_mueve_la_orden(
    db, clientes, hold_service, stripe_falso
):
    _, _, _, iniciada = await orden_iniciada(
        db, hold_service, stripe_falso, clientes[0].id
    )
    cliente = await db.get(Client, clientes[0].id)
    cliente.comision_pct = Decimal("25.00")
    cliente.cargo_orden = Decimal("999.00")
    await db.flush()

    orden = await recargar(db, Order, iniciada.orden.id)
    assert orden.comision_pct_snap == Decimal("10.00")
    assert orden.cargo_orden_snap == Decimal("50.00")


async def test_la_orden_vence_con_el_apartado_extendido(
    db, clientes, hold_service, stripe_falso
):
    """La orden no puede vivir mas que el apartado: el espejo manda."""
    esc, _, _, iniciada = await orden_iniciada(
        db, hold_service, stripe_falso, clientes[0].id
    )
    pid = esc["performance_id"]
    ttl = await hold_service.ttl_restante(pid, esc["luneta"][0])
    assert ttl > settings.HOLD_TTL_SEGUNDOS, "no se extendio para pagar"

    f = await fila(db, pid, esc["luneta"][0])
    assert iniciada.orden.expira_en == f.hold_expira_en
    ahora = await db.scalar(select(func.now()))
    assert f.hold_expira_en <= ahora + timedelta(seconds=ttl)


async def test_repetir_el_checkout_no_alarga_el_apartado_sin_fin(
    db, clientes, hold_service, stripe_falso
):
    """El tope cuenta desde la primera butaca, no desde el ultimo clic."""
    esc = await escenario(db, clientes[0].id)
    hold = await apartar(db, hold_service, esc, esc["luneta"][:2])
    tope = settings.HOLD_TTL_SEGUNDOS + settings.HOLD_TTL_CHECKOUT_SEGUNDOS
    await db.execute(
        update(InventoryMove)
        .where(InventoryMove.hold_id == hold)
        .values(creado_en=func.now() - timedelta(seconds=tope - 60))
    )

    await CheckoutService(db, hold_service, stripe_falso).iniciar(
        esc["performance_id"], hold, comprador()
    )

    ttl = await hold_service.ttl_restante(esc["performance_id"], esc["luneta"][0])
    assert ttl <= settings.HOLD_TTL_SEGUNDOS, "se extendio mas alla del tope"


async def test_doble_clic_es_la_misma_orden_y_un_solo_cobro(
    db, clientes, hold_service, stripe_falso
):
    esc, hold, servicio, primera = await orden_iniciada(
        db, hold_service, stripe_falso, clientes[0].id
    )
    segunda = await servicio.iniciar(esc["performance_id"], hold, comprador())

    assert segunda.orden.id == primera.orden.id
    assert segunda.client_secret == primera.client_secret
    assert len(stripe_falso.intentos) == 1


async def test_corregir_el_correo_reemplaza_la_orden(
    db, clientes, hold_service, stripe_falso
):
    esc, hold, servicio, primera = await orden_iniciada(
        db, hold_service, stripe_falso, clientes[0].id
    )
    segunda = await servicio.iniciar(
        esc["performance_id"], hold, comprador(email="ana.bien@correo.mx")
    )

    vieja = await recargar(db, Order, primera.orden.id)
    assert segunda.orden.id != vieja.id
    assert (vieja.estado, vieja.cerrada_motivo) == ("cancelada", "reemplazada")
    assert stripe_falso.intentos[vieja.stripe_payment_intent_id].estado == CANCELADO
    vivas = await db.scalar(
        select(func.count(OrderItem.id)).where(
            OrderItem.order_id == vieja.id, OrderItem.anulado.is_(False)
        )
    )
    assert vivas == 0


async def test_no_reemplaza_una_orden_ya_autorizada(
    db, clientes, hold_service, stripe_falso
):
    """Pago en otra pestaña: se termina con confirmar, no se cancela."""
    esc, hold, servicio, primera = await orden_iniciada(
        db, hold_service, stripe_falso, clientes[0].id
    )
    stripe_falso.autorizar(primera.orden.stripe_payment_intent_id)

    with pytest.raises(PagoEnProceso):
        await servicio.iniciar(
            esc["performance_id"], hold, comprador(email="otra@correo.mx")
        )
    assert stripe_falso.cancelaciones == []


async def test_sin_aceptar_el_aviso_no_hay_orden(
    db, clientes, hold_service, stripe_falso
):
    esc = await escenario(db, clientes[0].id)
    hold = await apartar(db, hold_service, esc, esc["luneta"][:2])

    with pytest.raises(ConsentimientoInvalido):
        await CheckoutService(db, hold_service, stripe_falso).iniciar(
            esc["performance_id"], hold, comprador(acepta_aviso=False)
        )
    assert stripe_falso.intentos == {}


async def test_telefono_invalido_se_rechaza_antes_de_todo(
    db, clientes, hold_service, stripe_falso
):
    esc = await escenario(db, clientes[0].id)
    hold = await apartar(db, hold_service, esc, esc["luneta"][:2])

    with pytest.raises(TelefonoInvalido):
        await CheckoutService(db, hold_service, stripe_falso).iniciar(
            esc["performance_id"], hold, comprador(telefono="1234")
        )
    assert stripe_falso.intentos == {}


async def test_apartado_vencido_no_llega_a_stripe(
    db, clientes, hold_service, stripe_falso, redis_test
):
    esc = await escenario(db, clientes[0].id)
    seats = esc["luneta"][:2]
    hold = await apartar(db, hold_service, esc, seats)
    for s in seats:
        await redis_test.delete(clave_butaca(esc["performance_id"], s))

    with pytest.raises(ApartadoExpirado):
        await CheckoutService(db, hold_service, stripe_falso).iniciar(
            esc["performance_id"], hold, comprador()
        )
    assert stripe_falso.intentos == {}


async def test_revalida_butacas_sueltas_al_pagar(
    db, clientes, hold_service, stripe_falso
):
    """Entre apartar y pagar, el vecino pudo cambiar.

    Al apartar 3 y 4 quedaban 1-2 libres. Si despues se vende la 1, pagar
    3 y 4 dejaria la 2 sola, y eso se pudo evitar eligiendo 2 y 3.
    """
    esc = await escenario(db, clientes[0].id, por_zona=8)
    l1, l2, l3, l4 = esc["luneta"][:4]
    hold = await apartar(db, hold_service, esc, [l3, l4])
    await poner_estado(db, esc["performance_id"], l1, EstadoAsiento.VENDIDO.value)

    with pytest.raises(DejaButacaSuelta) as exc:
        await CheckoutService(db, hold_service, stripe_falso).iniciar(
            esc["performance_id"], hold, comprador()
        )
    assert exc.value.detalle["seat_id"] == l2
    assert stripe_falso.intentos == {}


async def test_venta_cerrada_al_pagar_no_cobra(
    db, clientes, hold_service, stripe_falso
):
    esc = await escenario(db, clientes[0].id)
    hold = await apartar(db, hold_service, esc, esc["luneta"][:2])
    evento = await db.get(Event, esc["event_id"])
    evento.estado = EstadoEvento.BORRADOR.value
    await db.flush()

    with pytest.raises(Exception) as exc:
        await CheckoutService(db, hold_service, stripe_falso).iniciar(
            esc["performance_id"], hold, comprador()
        )
    assert getattr(exc.value, "http_status", None) == 404
    assert stripe_falso.intentos == {}


async def test_funcion_que_ya_no_vende_no_cobra(
    db, clientes, hold_service, stripe_falso
):
    from app.models.catalog import Performance

    esc = await escenario(db, clientes[0].id)
    hold = await apartar(db, hold_service, esc, esc["luneta"][:2])
    funcion = await db.get(Performance, esc["performance_id"])
    funcion.venta_cierra_en = await db.scalar(select(func.now()))
    await db.flush()

    with pytest.raises(FuncionNoALaVenta):
        await CheckoutService(db, hold_service, stripe_falso).iniciar(
            esc["performance_id"], hold, comprador()
        )


async def test_menos_de_diez_pesos_no_va_a_stripe(
    db, clientes, hold_service, stripe_falso
):
    cliente = await db.get(Client, clientes[0].id)
    cliente.cargo_orden = Decimal("0")
    cliente.cargo_servicio_pct = Decimal("0")
    esc = await escenario(db, clientes[0].id)
    await db.execute(
        update(PerformanceSeat)
        .where(PerformanceSeat.performance_id == esc["performance_id"])
        .values(precio_final=Decimal("4.00"))
    )
    hold = await apartar(db, hold_service, esc, esc["luneta"][:2])

    with pytest.raises(OrdenSinCobro):
        await CheckoutService(db, hold_service, stripe_falso).iniciar(
            esc["performance_id"], hold, comprador()
        )
    assert stripe_falso.intentos == {}


async def test_orden_ajena_abandonada_no_bloquea_la_butaca(
    db, clientes, hold_service, stripe_falso, redis_test
):
    """Alguien inicio el pago y se fue. Su apartado vencio; la butaca es
    de quien llega despues, y la orden abandonada se cierra sola."""
    esc, hold_a, _, abandonada = await orden_iniciada(
        db, hold_service, stripe_falso, clientes[0].id
    )
    pid, seats = esc["performance_id"], esc["luneta"][:2]
    for s in seats:
        await redis_test.delete(clave_butaca(pid, s))
    await db.execute(
        update(PerformanceSeat)
        .where(PerformanceSeat.hold_id == hold_a)
        .values(hold_expira_en=func.now() - timedelta(seconds=1))
    )
    await db.execute(
        update(Order)
        .where(Order.id == abandonada.orden.id)
        .values(expira_en=func.now() - timedelta(seconds=1))
    )

    hold_b = await apartar(db, hold_service, esc, seats)
    nueva = await CheckoutService(db, hold_service, stripe_falso).iniciar(
        pid, hold_b, comprador(email="beto@correo.mx")
    )

    vieja = await recargar(db, Order, abandonada.orden.id)
    assert (vieja.estado, vieja.cerrada_motivo) == ("expirada", "apartado_vencido")
    assert nueva.orden.estado == "pendiente"


# --------------------------------------------------------------------------
# Comprador y consentimientos
# --------------------------------------------------------------------------


async def test_comprador_normalizado_y_consentimiento_de_operacion(
    db, clientes, hold_service, stripe_falso
):
    _, _, _, iniciada = await orden_iniciada(
        db, hold_service, stripe_falso, clientes[0].id
    )
    persona = await db.get(Customer, iniciada.orden.customer_id)
    assert persona.email == "ana.lopez@correo.mx"
    assert persona.telefono == "+525512345678"

    registros = (
        await db.scalars(select(Consent).where(Consent.customer_id == persona.id))
    ).all()
    assert [(c.finalidad, c.otorgado, c.origen, c.order_id) for c in registros] == [
        ("operacion", True, "checkout_web", iniciada.orden.id)
    ], "sin marcar la casilla no se registra marketing"


async def test_marketing_solo_con_su_casilla(db, clientes, hold_service, stripe_falso):
    _, _, _, iniciada = await orden_iniciada(
        db, hold_service, stripe_falso, clientes[0].id, acepta_marketing=True
    )
    finalidades = set(
        (
            await db.scalars(
                select(Consent.finalidad).where(
                    Consent.customer_id == iniciada.orden.customer_id
                )
            )
        ).all()
    )
    assert finalidades == {"operacion", "marketing"}


async def test_un_correo_ajeno_no_cambia_los_datos_de_su_dueno(
    db, clientes, hold_service, stripe_falso
):
    """Cualquiera puede escribir un correo ajeno en un checkout."""
    db.add(Customer(email="ana.lopez@correo.mx", nombre=None, telefono="+523312345678"))
    await db.flush()

    _, _, _, iniciada = await orden_iniciada(
        db, hold_service, stripe_falso, clientes[0].id, telefono="55 9999 8888"
    )
    persona = await db.get(Customer, iniciada.orden.customer_id)
    assert persona.telefono == "+523312345678"
    assert persona.nombre == "Ana", "lo que faltaba si se completa"


# --------------------------------------------------------------------------
# Finalizar
# --------------------------------------------------------------------------


async def test_autorizado_vende_emite_y_captura(
    db, clientes, hold_service, stripe_falso, redis_test
):
    esc, _, servicio, iniciada = await orden_iniciada(
        db, hold_service, stripe_falso, clientes[0].id
    )
    pi = iniciada.orden.stripe_payment_intent_id
    stripe_falso.autorizar(pi)

    orden = await servicio.finalizar(iniciada.orden.id)

    assert orden.estado == "pagada"
    assert orden.pagada_en is not None
    assert orden.capturada_en is not None
    assert stripe_falso.capturas == [pi]

    for s in esc["luneta"][:2]:
        f = await fila(db, esc["performance_id"], s)
        assert (f.estado, f.hold_id, f.hold_expira_en) == ("vendido", None, None)
        assert not await redis_test.exists(clave_butaca(esc["performance_id"], s))

    boletos = (
        await db.scalars(
            select(Ticket)
            .join(OrderItem, OrderItem.id == Ticket.order_item_id)
            .where(OrderItem.order_id == orden.id)
        )
    ).all()
    assert len(boletos) == 2
    assert all(re.fullmatch(r"[0-9A-Z]{5}-[0-9A-Z]{5}", b.codigo) for b in boletos)
    assert all(len(b.semilla) == 32 for b in boletos)
    assert all(b.holder_customer_id == orden.customer_id for b in boletos)

    movimientos = (
        await db.scalars(
            select(InventoryMove.motivo).where(InventoryMove.order_id == orden.id)
        )
    ).all()
    assert movimientos == ["vendido", "vendido"]


async def test_la_venta_queda_en_la_bandeja_sin_datos_personales(
    db, clientes, hold_service, stripe_falso
):
    _, _, servicio, iniciada = await orden_iniciada(
        db, hold_service, stripe_falso, clientes[0].id
    )
    stripe_falso.autorizar(iniciada.orden.stripe_payment_intent_id)
    orden = await servicio.finalizar(iniciada.orden.id)

    hecho = await db.scalar(
        select(OutboxEvent).where(OutboxEvent.agregado_uuid == orden.uuid)
    )
    assert hecho.tipo == "orden.pagada"
    assert hecho.publicado_en is None
    assert hecho.payload["orden_uuid"] == str(orden.uuid)
    assert hecho.payload["total"] == "1810.00"
    assert len(hecho.payload["boletos"]) == 2
    texto = json.dumps(hecho.payload)
    assert "correo.mx" not in texto and "+52" not in texto


async def test_finalizar_dos_veces_no_duplica_nada(
    db, clientes, hold_service, stripe_falso
):
    _, _, servicio, iniciada = await orden_iniciada(
        db, hold_service, stripe_falso, clientes[0].id
    )
    stripe_falso.autorizar(iniciada.orden.stripe_payment_intent_id)

    await servicio.finalizar(iniciada.orden.id)
    await servicio.finalizar(iniciada.orden.id)

    assert len(stripe_falso.capturas) == 1
    boletos = await db.scalar(
        select(func.count(Ticket.id))
        .join(OrderItem, OrderItem.id == Ticket.order_item_id)
        .where(OrderItem.order_id == iniciada.orden.id)
    )
    assert boletos == 2


async def test_sin_autorizar_no_se_vende(db, clientes, hold_service, stripe_falso):
    esc, _, servicio, iniciada = await orden_iniciada(
        db, hold_service, stripe_falso, clientes[0].id
    )

    orden = await servicio.finalizar(iniciada.orden.id)

    assert orden.estado == "pendiente"
    assert stripe_falso.capturas == []
    f = await fila(db, esc["performance_id"], esc["luneta"][0])
    assert f.estado == "apartado"


async def test_apartado_perdido_cancela_la_autorizacion(
    db, clientes, hold_service, stripe_falso, redis_test
):
    """Pago tarde: no hay boleto y tampoco cargo."""
    esc, _, servicio, iniciada = await orden_iniciada(
        db, hold_service, stripe_falso, clientes[0].id
    )
    pi = iniciada.orden.stripe_payment_intent_id
    stripe_falso.autorizar(pi)
    await redis_test.delete(clave_butaca(esc["performance_id"], esc["luneta"][0]))

    orden = await servicio.finalizar(iniciada.orden.id)

    assert (orden.estado, orden.cerrada_motivo) == ("expirada", "apartado_vencido")
    assert stripe_falso.cancelaciones == [pi]
    assert stripe_falso.capturas == []
    assert await db.scalar(select(func.count(Ticket.id))) == 0
    for s in esc["luneta"][:2]:
        assert (await fila(db, esc["performance_id"], s)).estado != "vendido"


async def test_si_postgres_perdio_una_butaca_no_vende_ninguna(
    db, clientes, hold_service, stripe_falso
):
    """Capa 2: todo o nada. Una butaca vendida a medias seria peor."""
    esc, _, servicio, iniciada = await orden_iniciada(
        db, hold_service, stripe_falso, clientes[0].id
    )
    pid, (l1, l2) = esc["performance_id"], esc["luneta"][:2]
    stripe_falso.autorizar(iniciada.orden.stripe_payment_intent_id)
    await db.execute(
        update(PerformanceSeat)
        .where(PerformanceSeat.performance_id == pid, PerformanceSeat.seat_id == l2)
        .values(hold_id="otro-dueno")
    )

    orden = await servicio.finalizar(iniciada.orden.id)

    assert orden.estado == "expirada"
    assert (await fila(db, pid, l1)).estado == "apartado", "l1 quedo vendida sola"
    assert stripe_falso.capturas == []
    assert len(stripe_falso.cancelaciones) == 1


async def test_stripe_caido_al_capturar_no_cancela_boletos(
    db, clientes, hold_service, stripe_falso
):
    """La orden queda pagada sin capturar, y el reintento la cobra."""
    _, _, servicio, iniciada = await orden_iniciada(
        db, hold_service, stripe_falso, clientes[0].id
    )
    stripe_falso.autorizar(iniciada.orden.stripe_payment_intent_id)
    stripe_falso.falla_captura = PasarelaNoDisponible("Stripe no responde")

    with pytest.raises(PasarelaNoDisponible):
        await servicio.finalizar(iniciada.orden.id)

    orden = await recargar(db, Order, iniciada.orden.id)
    assert (orden.estado, orden.capturada_en) == ("pagada", None)
    validos = await db.scalar(
        select(func.count(Ticket.id)).where(Ticket.estado == "valido")
    )
    assert validos == 2

    stripe_falso.falla_captura = None
    orden = await servicio.finalizar(iniciada.orden.id)
    assert orden.capturada_en is not None
    assert len(stripe_falso.capturas) == 1


async def test_captura_rechazada_deshace_la_venta(
    db, clientes, hold_service, stripe_falso
):
    esc, _, servicio, iniciada = await orden_iniciada(
        db, hold_service, stripe_falso, clientes[0].id
    )
    stripe_falso.autorizar(iniciada.orden.stripe_payment_intent_id)
    stripe_falso.falla_captura = CobroRechazado("Autorizacion vencida")

    orden = await servicio.finalizar(iniciada.orden.id)

    assert (orden.estado, orden.cerrada_motivo) == ("cancelada", "captura_rechazada")
    estados = set((await db.scalars(select(Ticket.estado))).all())
    assert estados == {"cancelado"}
    for s in esc["luneta"][:2]:
        assert (await fila(db, esc["performance_id"], s)).estado == "disponible"
    vivas = await db.scalar(
        select(func.count(OrderItem.id)).where(
            OrderItem.order_id == orden.id, OrderItem.anulado.is_(False)
        )
    )
    assert vivas == 0, "la butaca no se podria volver a vender"
    tipos = (
        await db.scalars(
            select(OutboxEvent.tipo)
            .where(OutboxEvent.agregado_uuid == orden.uuid)
            .order_by(OutboxEvent.id)
        )
    ).all()
    assert tipos == ["orden.pagada", "orden.cancelada"]


async def test_autorizacion_tardia_sobre_orden_cerrada_se_suelta(
    db, clientes, hold_service, stripe_falso
):
    _, _, servicio, iniciada = await orden_iniciada(
        db, hold_service, stripe_falso, clientes[0].id
    )
    pi = iniciada.orden.stripe_payment_intent_id
    await db.execute(
        update(Order)
        .where(Order.id == iniciada.orden.id)
        .values(estado="expirada", cerrada_motivo="apartado_vencido")
    )
    stripe_falso.autorizar(pi)

    await servicio.finalizar(iniciada.orden.id)

    assert stripe_falso.cancelaciones == [pi]
    assert stripe_falso.capturas == []


async def test_intento_cancelado_cierra_la_orden(
    db, clientes, hold_service, stripe_falso
):
    _, _, servicio, iniciada = await orden_iniciada(
        db, hold_service, stripe_falso, clientes[0].id
    )
    stripe_falso.poner(iniciada.orden.stripe_payment_intent_id, estado=CANCELADO)

    orden = await servicio.finalizar(iniciada.orden.id)

    assert (orden.estado, orden.cerrada_motivo) == ("cancelada", "pago_cancelado")


async def test_cobrado_por_fuera_se_vende_sin_cobrar_otra_vez(
    db, clientes, hold_service, stripe_falso
):
    """Alguien capturo a mano en el panel de Stripe: hay que emitir."""
    _, _, servicio, iniciada = await orden_iniciada(
        db, hold_service, stripe_falso, clientes[0].id
    )
    stripe_falso.poner(iniciada.orden.stripe_payment_intent_id, estado=COBRADO)

    orden = await servicio.finalizar(iniciada.orden.id)

    assert orden.estado == "pagada"
    assert orden.capturada_en is not None
    assert stripe_falso.capturas == []
    assert await db.scalar(select(func.count(Ticket.id))) == 2


async def test_cobrado_sin_butaca_queda_marcado_para_reembolso(
    db, clientes, hold_service, stripe_falso, redis_test
):
    """Dinero cobrado y sin boletos: nunca se cierra en silencio."""
    esc, _, servicio, iniciada = await orden_iniciada(
        db, hold_service, stripe_falso, clientes[0].id
    )
    stripe_falso.poner(iniciada.orden.stripe_payment_intent_id, estado=COBRADO)
    await redis_test.delete(clave_butaca(esc["performance_id"], esc["luneta"][0]))

    orden = await servicio.finalizar(iniciada.orden.id)
    assert orden.cerrada_motivo == "requiere_reembolso", "al primer intento"

    orden = await servicio.finalizar(iniciada.orden.id)
    assert orden.cerrada_motivo == "requiere_reembolso"
    assert await db.scalar(select(func.count(Ticket.id))) == 0
    avisos = (
        await db.scalars(
            select(OutboxEvent.tipo).where(OutboxEvent.agregado_uuid == orden.uuid)
        )
    ).all()
    assert avisos == ["orden.requiere_reembolso"], "un solo aviso"


@pytest.mark.parametrize(
    "cambio",
    [
        {"monto_centavos": 100},
        {"moneda": "USD"},
        {"metadata": {"orden_uuid": "de-otra-orden"}},
    ],
)
async def test_un_pago_que_no_corresponde_no_emite(
    db, clientes, hold_service, stripe_falso, cambio
):
    _, _, servicio, iniciada = await orden_iniciada(
        db, hold_service, stripe_falso, clientes[0].id
    )
    pi = iniciada.orden.stripe_payment_intent_id
    stripe_falso.autorizar(pi)
    stripe_falso.poner(pi, **cambio)

    with pytest.raises(IntentoNoCorresponde):
        await servicio.finalizar(iniciada.orden.id)
    assert await db.scalar(select(func.count(Ticket.id))) == 0
    assert stripe_falso.capturas == []


# --------------------------------------------------------------------------
# Concurrencia: webhook y front al mismo tiempo
# --------------------------------------------------------------------------


async def test_webhook_y_front_a_la_vez_un_solo_juego_de_boletos(
    motor_con_commit, hold_service, stripe_falso
):
    from sqlalchemy.ext.asyncio import AsyncSession

    async with AsyncSession(motor_con_commit, expire_on_commit=False) as s:
        cliente = Client(nombre="Concurrencia checkout")
        s.add(cliente)
        await s.flush()
        esc = await escenario(s, cliente.id)
        hold = await apartar(s, hold_service, esc, esc["luneta"][:2])
        iniciada = await CheckoutService(s, hold_service, stripe_falso).iniciar(
            esc["performance_id"], hold, comprador()
        )
        await s.commit()
    orden_id = iniciada.orden.id
    stripe_falso.autorizar(iniciada.orden.stripe_payment_intent_id)

    async def finalizar():
        async with AsyncSession(motor_con_commit, expire_on_commit=False) as s:
            await CheckoutService(s, hold_service, stripe_falso).finalizar(orden_id)

    await asyncio.gather(*(finalizar() for _ in range(5)))

    async with AsyncSession(motor_con_commit) as s:
        boletos = await s.scalar(select(func.count(Ticket.id)))
        orden = await s.get(Order, orden_id)
    assert boletos == 2
    assert orden.estado == "pagada"
    assert orden.capturada_en is not None
    assert len(stripe_falso.capturas) == 1


async def test_dos_clics_simultaneos_una_sola_orden(
    motor_con_commit, hold_service, stripe_falso
):
    from sqlalchemy.ext.asyncio import AsyncSession

    async with AsyncSession(motor_con_commit, expire_on_commit=False) as s:
        cliente = Client(nombre="Doble clic")
        s.add(cliente)
        await s.flush()
        esc = await escenario(s, cliente.id)
        hold = await apartar(s, hold_service, esc, esc["luneta"][:2])
        await s.commit()

    async def clic():
        async with AsyncSession(motor_con_commit, expire_on_commit=False) as s:
            try:
                r = await CheckoutService(s, hold_service, stripe_falso).iniciar(
                    esc["performance_id"], hold, comprador()
                )
                await s.commit()
                return r.orden.id
            except PagoEnProceso:
                await s.rollback()
                return None

    resultados = await asyncio.gather(*(clic() for _ in range(4)))

    async with AsyncSession(motor_con_commit) as s:
        ordenes = await s.scalar(select(func.count(Order.id)))
    assert ordenes == 1
    assert len(stripe_falso.intentos) == 1
    assert len({r for r in resultados if r is not None}) == 1


# --------------------------------------------------------------------------
# API y webhook
# --------------------------------------------------------------------------


def firmar(payload: bytes) -> str:
    t = int(time.time())
    firma = hmac.new(
        SECRETO.encode(), f"{t}.".encode() + payload, hashlib.sha256
    ).hexdigest()
    return f"t={t},v1={firma}"


def evento_de_stripe(pi: str, tipo="payment_intent.amount_capturable_updated"):
    return {
        "id": f"evt_{uuid.uuid4().hex[:12]}",
        "type": tipo,
        "data": {"object": {"id": pi, "object": "payment_intent"}},
    }


@pytest.fixture
def api(cliente_http, hold_service, stripe_falso):
    from app.api.v1.apartados import get_hold_service
    from app.api.v1.checkout import get_pasarela
    from app.main import app

    app.dependency_overrides[get_hold_service] = lambda: hold_service
    app.dependency_overrides[get_pasarela] = lambda: stripe_falso
    return cliente_http


CUERPO = {
    "email": "ana@correo.mx",
    "nombre": "Ana",
    "telefono": "55 1234 5678",
    "acepta_aviso": True,
    "aviso_version": "2026-10",
}


async def test_checkout_por_api_y_lo_que_ve_el_comprador(
    api, db, clientes, hold_service, monkeypatch
):
    monkeypatch.setattr(settings, "STRIPE_PUBLISHABLE_KEY", "pk_test_abc")
    esc = await escenario(db, clientes[0].id)
    hold = uuid.uuid4()
    await apartar(db, hold_service, esc, esc["luneta"][:2], hold=hold.hex)

    r = await api.post(
        f"{API}/performances/{esc['performance_id']}/apartados/{hold}/checkout",
        json=CUERPO,
    )

    assert r.status_code == 201, r.text
    cuerpo = r.json()
    assert set(cuerpo) == {"orden", "pago"}
    assert set(cuerpo["orden"]) == {
        "uuid",
        "folio",
        "estado",
        "moneda",
        "subtotal",
        "cargo_servicio",
        "cargo_orden",
        "descuento",
        "total",
        "expira_en",
        "pagada_en",
        "partidas",
    }
    assert set(cuerpo["orden"]["partidas"][0]) == {
        "zona",
        "asiento",
        "precio",
        "cargo_servicio",
        "importe",
    }
    assert cuerpo["orden"]["total"] == "1810.00"
    assert cuerpo["pago"] == {
        "proveedor": "stripe",
        "client_secret": "pi_1_secret_x",
        "publishable_key": "pk_test_abc",
    }

    orden = (await api.get(f"{API}/ordenes/{cuerpo['orden']['uuid']}")).json()
    assert orden["estado"] == "pendiente"
    assert "ana@correo.mx" not in json.dumps(orden)


async def test_orden_inexistente_es_404(api):
    r = await api.get(f"{API}/ordenes/{uuid.uuid4()}")
    assert r.status_code == 404
    assert r.json()["error"]["codigo"] == "no_encontrado"


async def test_confirmar_por_api(api, db, clientes, hold_service, stripe_falso):
    _, _, _, iniciada = await orden_iniciada(
        db, hold_service, stripe_falso, clientes[0].id
    )
    stripe_falso.autorizar(iniciada.orden.stripe_payment_intent_id)

    r = await api.post(f"{API}/ordenes/{iniciada.orden.uuid}/confirmar")

    assert r.status_code == 200, r.text
    assert r.json()["estado"] == "pagada"


async def test_webhook_firmado_vende_y_el_repetido_no_hace_nada(
    api, db, clientes, hold_service, stripe_falso, monkeypatch
):
    _, _, _, iniciada = await orden_iniciada(
        db, hold_service, stripe_falso, clientes[0].id
    )
    pi = iniciada.orden.stripe_payment_intent_id
    stripe_falso.autorizar(pi)
    payload = json.dumps(evento_de_stripe(pi)).encode()
    cabeceras = {"stripe-signature": firmar(payload)}

    r1 = await api.post(
        f"{API}/pagos/stripe/webhook", content=payload, headers=cabeceras
    )
    r2 = await api.post(
        f"{API}/pagos/stripe/webhook", content=payload, headers=cabeceras
    )

    assert r1.json()["resultado"] == "procesado"
    assert r2.json()["resultado"] == "duplicado"
    assert (await recargar(db, Order, iniciada.orden.id)).estado == "pagada"
    assert len(stripe_falso.capturas) == 1
    assert await db.scalar(select(func.count(PaymentEvent.id))) == 1


async def test_webhook_sin_firma_no_toca_nada(
    api, db, clientes, hold_service, stripe_falso
):
    _, _, _, iniciada = await orden_iniciada(
        db, hold_service, stripe_falso, clientes[0].id
    )
    pi = iniciada.orden.stripe_payment_intent_id
    stripe_falso.autorizar(pi)
    payload = json.dumps(evento_de_stripe(pi)).encode()

    r = await api.post(
        f"{API}/pagos/stripe/webhook",
        content=payload,
        headers={"stripe-signature": "t=1,v1=inventada"},
    )

    assert r.status_code == 400
    assert r.json()["error"]["codigo"] == "firma_invalida"
    assert (await recargar(db, Order, iniciada.orden.id)).estado == "pendiente"
    assert await db.scalar(select(func.count(PaymentEvent.id))) == 0


async def test_webhook_de_otro_tipo_se_guarda_y_se_ignora(api, db):
    evento = {"id": "evt_otro", "type": "customer.created", "data": {"object": {}}}
    payload = json.dumps(evento).encode()

    r = await api.post(
        f"{API}/pagos/stripe/webhook",
        content=payload,
        headers={"stripe-signature": firmar(payload)},
    )

    assert r.json()["resultado"] == "ignorado"
    registro = await db.scalar(select(PaymentEvent))
    assert registro.tipo == "customer.created"
    assert registro.procesado_en is not None


async def test_webhook_que_falla_se_anota_y_stripe_reintenta(
    motor_con_commit, hold_service, stripe_falso
):
    """Si procesar falla se contesta error, y el reintento si procesa."""
    from httpx import ASGITransport, AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.api.v1.apartados import get_hold_service
    from app.api.v1.checkout import get_pasarela
    from app.core.db import get_db
    from app.main import app

    async with AsyncSession(motor_con_commit, expire_on_commit=False) as s:
        cliente = Client(nombre="Webhook con falla")
        s.add(cliente)
        await s.flush()
        esc = await escenario(s, cliente.id)
        hold = await apartar(s, hold_service, esc, esc["luneta"][:2])
        iniciada = await CheckoutService(s, hold_service, stripe_falso).iniciar(
            esc["performance_id"], hold, comprador()
        )
        await s.commit()
    pi = iniciada.orden.stripe_payment_intent_id
    stripe_falso.autorizar(pi)

    async def _get_db():
        async with AsyncSession(motor_con_commit, expire_on_commit=False) as s:
            yield s

    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[get_hold_service] = lambda: hold_service
    app.dependency_overrides[get_pasarela] = lambda: stripe_falso
    payload = json.dumps(evento_de_stripe(pi)).encode()
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as c:
            stripe_falso.falla_obtener = PasarelaNoDisponible("Stripe no responde")
            r1 = await c.post(
                f"{API}/pagos/stripe/webhook",
                content=payload,
                headers={"stripe-signature": firmar(payload)},
            )
            async with AsyncSession(motor_con_commit) as s:
                registro = await s.scalar(select(PaymentEvent))
            assert r1.status_code == 502
            assert registro.procesado_en is None
            assert "Stripe no responde" in registro.error

            stripe_falso.falla_obtener = None
            r2 = await c.post(
                f"{API}/pagos/stripe/webhook",
                content=payload,
                headers={"stripe-signature": firmar(payload)},
            )
    finally:
        app.dependency_overrides.clear()

    assert r2.json()["resultado"] == "procesado"
    async with AsyncSession(motor_con_commit) as s:
        orden = await s.get(Order, iniciada.orden.id)
    assert orden.estado == "pagada"
