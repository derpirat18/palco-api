"""Checkout: de un apartado a una orden pagada con boletos.

Zona critica. Aqui entra el dinero, asi que el orden de los pasos es la
garantia (ADR 0010):

  iniciar
    1. La funcion se sigue vendiendo y el apartado sigue vivo en Redis.
    2. La seleccion no deja butacas sueltas (pudo cambiar desde que se
       aparto: alguien libero una butaca al lado).
    3. Se extiende el apartado para cubrir el pago, con tope total.
    4. Comprador, orden con importes congelados, partidas, consentimientos.
    5. Stripe autoriza despues, con captura manual.

  finalizar (webhook o el front al volver de Stripe; da igual cuantas veces)
    1. Se consulta a Stripe el estado real. El webhook solo avisa.
    2. Autorizado: el apartado sigue siendo nuestro en Redis y Postgres
       pasa las butacas a vendido con un UPDATE condicional (capa 2).
    3. Boletos, orden pagada y hecho en la bandeja de salida, y se
       confirma la transaccion.
    4. Recien entonces se captura el cobro.

Si en el paso 2 la butaca ya no es nuestra, se cancela la autorizacion:
el comprador nunca ve un cargo. Si la captura se rechaza en definitiva,
se deshace la venta. Si Stripe solo tropieza, la orden queda pagada sin
capturar y se reintenta: nunca se cancelan boletos por un error de red.

Las transacciones de `finalizar` las maneja este servicio, porque entre
emitir y capturar tiene que haber un commit.
"""

from __future__ import annotations

import contextlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from uuid import UUID

from sqlalchemy import func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import (
    ApartadoExpirado,
    AsientoNoDisponible,
    DejaButacaSuelta,
    ErrorDeDominio,
    RecursoNoEncontrado,
)
from app.core.telefonos import normalizar_telefono
from app.models.base import EstadoAsiento, EstadoBoleto, EstadoOrden
from app.models.catalog import Client, Event, Performance, Seat, Zone
from app.models.inventory import InventoryMove, PerformanceSeat
from app.models.outbox import OutboxEvent
from app.models.sales import Customer, Order, OrderItem, Ticket
from app.services.apartados import MARGEN_ESPEJO_SEGUNDOS, ApartadoService
from app.services.consentimientos import ConsentimientoInvalido, ConsentimientoService
from app.services.holds import HoldService
from app.services.pasarela import (
    AUTORIZADO,
    CANCELADO,
    COBRADO,
    CobroRechazado,
    IntentoDePago,
    Pasarela,
)
from app.services.venta import CENTAVO, cargos_de

MONEDA = "MXN"
# Stripe no acepta cobros menores a 10 pesos. Una orden en cero es una
# cortesia y va por otro camino.
TOTAL_MINIMO = Decimal("10.00")
ORIGEN_CONSENTIMIENTO = "checkout_web"
ORDEN_NO_EXISTE = "La orden no existe."
REQUIERE_REEMBOLSO = "requiere_reembolso"

APARTADO = EstadoAsiento.APARTADO.value
VENDIDO = EstadoAsiento.VENDIDO.value
DISPONIBLE = EstadoAsiento.DISPONIBLE.value
PENDIENTE = EstadoOrden.PENDIENTE.value
PAGADA = EstadoOrden.PAGADA.value
CANCELADA = EstadoOrden.CANCELADA.value
EXPIRADA = EstadoOrden.EXPIRADA.value

# Crockford base 32: sin I, L, O ni U, que se confunden al dictarlos.
_ALFABETO = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _azar(n: int) -> str:
    return "".join(secrets.choice(_ALFABETO) for _ in range(n))


def nuevo_folio() -> str:
    return f"PAL-{_azar(8)}"


def nuevo_codigo_de_boleto() -> str:
    c = _azar(10)
    return f"{c[:5]}-{c[5:]}"


def centavos(monto: Decimal) -> int:
    return int((monto.quantize(CENTAVO) * 100).to_integral_value())


class OrdenSinCobro(ErrorDeDominio):
    codigo = "orden_sin_cobro"
    http_status = 422


class PagoEnProceso(ErrorDeDominio):
    """Ya hay un pago autorizado para este apartado: no se abre otro."""

    codigo = "pago_en_proceso"
    http_status = 409


class IntentoNoCorresponde(ErrorDeDominio):
    """El PaymentIntent no es de esta orden o no cobra lo mismo.

    No deberia pasar nunca. Si pasa, no se vende nada: es mejor una orden
    atorada que boletos emitidos por un cobro de otro monto.
    """

    codigo = "intento_no_corresponde"
    http_status = 409


@dataclass(frozen=True)
class DatosDelComprador:
    email: str
    nombre: str
    apellidos: str | None
    telefono: str | None
    acepta_aviso: bool
    aviso_version: str
    acepta_marketing: bool = False


@dataclass(frozen=True)
class Atribucion:
    utm_source: str | None = None
    utm_medium: str | None = None
    utm_campaign: str | None = None
    utm_content: str | None = None
    canal: str | None = None


@dataclass(frozen=True)
class OrdenIniciada:
    orden: Order
    partidas: list[OrderItem]
    client_secret: str


async def consultar_orden(
    db: AsyncSession, orden_uuid: UUID
) -> tuple[Order, list[OrderItem]]:
    """La orden con todas sus partidas, tambien las anuladas."""
    orden = await db.scalar(
        select(Order)
        .where(Order.uuid == orden_uuid)
        .execution_options(populate_existing=True)
    )
    if orden is None:
        raise RecursoNoEncontrado(ORDEN_NO_EXISTE)
    partidas = list(
        (
            await db.scalars(
                select(OrderItem)
                .where(OrderItem.order_id == orden.id)
                .order_by(OrderItem.id)
                .execution_options(populate_existing=True)
            )
        ).all()
    )
    return orden, partidas


class CheckoutService:
    def __init__(
        self, db: AsyncSession, holds: HoldService, pasarela: Pasarela
    ) -> None:
        self.db = db
        self.holds = holds
        self.pasarela = pasarela
        self.apartados = ApartadoService(db, holds)

    # ================================================================ iniciar

    async def iniciar(
        self,
        performance_id: int,
        hold_id: str,
        comprador: DatosDelComprador,
        atribucion: Atribucion | None = None,
    ) -> OrdenIniciada:
        """Convierte el apartado en una orden esperando pago.

        Repetirla con lo mismo devuelve la misma orden. Si cambio la
        seleccion o el correo, la orden anterior se cancela y se abre otra.
        """
        if not comprador.acepta_aviso:
            raise ConsentimientoInvalido(
                "Para comprar hay que aceptar el aviso de privacidad."
            )
        email = comprador.email.strip().lower()
        telefono = normalizar_telefono(comprador.telefono)

        ahora = await self._ahora()
        _, evento, cliente = await self.apartados.funcion_a_la_venta(
            performance_id, ahora
        )

        try:
            apartado = await self.apartados.consultar(performance_id, hold_id)
        except RecursoNoEncontrado as exc:
            raise ApartadoExpirado(
                "Tu apartado vencio. Elige tus butacas de nuevo.",
                {"performance_id": performance_id},
            ) from exc
        seat_ids = [b.seat_id for b in apartado.butacas]
        ps_ids = sorted(b.performance_seat_id for b in apartado.butacas)

        suelta = await self.apartados.butaca_que_quedaria_suelta(
            performance_id, seat_ids, hold_id, ahora
        )
        if suelta is not None:
            raise DejaButacaSuelta(
                "Tu seleccion deja una butaca sola. Ajusta tus butacas antes de pagar.",
                {"seat_id": suelta, "performance_id": performance_id},
            )

        previa = await self.db.scalar(
            select(Order)
            .where(Order.hold_id == hold_id, Order.estado == PENDIENTE)
            .with_for_update()
        )
        if previa is not None:
            reanudada = await self._reanudar_si_es_la_misma(previa, ps_ids, email)
            if reanudada is not None:
                return reanudada

        expira_en = await self._extender(performance_id, seat_ids, hold_id, ahora)
        await self._cerrar_vencidas(ps_ids)

        detalle = await self._detalle_de_butacas(ps_ids)
        cargos = cargos_de(evento, cliente)
        partidas = []
        for ps_id, precio, zona, fila, numero in detalle:
            con_cargo = cargos.con_cargo_de_servicio(precio)
            partidas.append(
                OrderItem(
                    performance_seat_id=ps_id,
                    zona_snap=zona,
                    asiento_snap=f"Fila {fila} · {numero}",
                    precio_unitario=precio,
                    cargo_servicio=con_cargo - precio,
                    descuento=Decimal("0"),
                    importe=con_cargo,
                )
            )
        subtotal = sum((p.precio_unitario for p in partidas), Decimal("0"))
        cargo_servicio = sum((p.cargo_servicio for p in partidas), Decimal("0"))
        total = subtotal + cargo_servicio + cargos.por_orden
        if total < TOTAL_MINIMO:
            raise OrdenSinCobro(
                "Una compra con pago debe ser de al menos $10.00.",
                {"total": str(total)},
            )

        persona = await self._comprador(email, comprador, telefono)
        a = atribucion or Atribucion()
        orden = Order(
            folio=nuevo_folio(),
            customer_id=persona.id,
            performance_id=performance_id,
            estado=PENDIENTE,
            subtotal=subtotal,
            cargo_servicio=cargo_servicio,
            cargo_orden=cargos.por_orden,
            descuento=Decimal("0"),
            total=total,
            moneda=MONEDA,
            comision_pct_snap=cliente.comision_pct,
            cargo_servicio_pct_snap=cargos.servicio_pct,
            cargo_orden_snap=cargos.por_orden,
            idempotency_key=secrets.token_hex(16),
            expira_en=expira_en,
            hold_id=hold_id,
            utm_source=a.utm_source,
            utm_medium=a.utm_medium,
            utm_campaign=a.utm_campaign,
            utm_content=a.utm_content,
            canal=a.canal,
        )
        try:
            async with self.db.begin_nested():
                self.db.add(orden)
                await self.db.flush()
        except IntegrityError as exc:
            # Dos clics al mismo tiempo sobre el mismo apartado: el otro
            # gano. Repetir devuelve su orden.
            raise PagoEnProceso(
                "Ya se esta preparando el pago de estas butacas. Intenta de "
                "nuevo en un momento.",
                {"performance_id": performance_id},
            ) from exc

        for p in partidas:
            p.order_id = orden.id
        try:
            async with self.db.begin_nested():
                self.db.add_all(partidas)
                await self.db.flush()
        except IntegrityError as exc:
            # Capa 3: otra orden viva tiene una de estas butacas.
            raise AsientoNoDisponible(
                "Una de tus butacas ya esta en otra compra.",
                {"performance_id": performance_id},
            ) from exc

        consentimientos = ConsentimientoService(self.db)
        await consentimientos.registrar(
            persona.id,
            "operacion",
            True,
            ORIGEN_CONSENTIMIENTO,
            comprador.aviso_version,
            order_id=orden.id,
        )
        # Sin marcar la casilla no se registra nada: no marcarla al
        # comprar no es retirar un permiso que se dio antes.
        if comprador.acepta_marketing:
            await consentimientos.registrar(
                persona.id,
                "marketing",
                True,
                ORIGEN_CONSENTIMIENTO,
                comprador.aviso_version,
                order_id=orden.id,
            )

        intento = await self.pasarela.crear_intento(
            monto_centavos=centavos(total),
            moneda=MONEDA,
            descripcion=f"Palco {orden.folio} · {evento.nombre}"[:200],
            metadata={
                "orden_uuid": str(orden.uuid),
                "folio": orden.folio,
                "hold_id": hold_id,
            },
            idempotency_key=f"orden-{orden.uuid}",
        )
        orden.stripe_payment_intent_id = intento.id
        await self.db.flush()
        return OrdenIniciada(orden, partidas, intento.client_secret or "")

    # ============================================================== finalizar

    async def finalizar_por_intento(self, intento_id: str) -> Order | None:
        orden_id = await self.db.scalar(
            select(Order.id).where(Order.stripe_payment_intent_id == intento_id)
        )
        if orden_id is None:
            return None
        return await self.finalizar(orden_id)

    async def finalizar(self, orden_id: int) -> Order:
        """Lleva la orden a donde dice Stripe. Idempotente.

        Se puede llamar las veces que sea, desde el webhook o desde el
        front, incluso al mismo tiempo: el candado de la orden las pone en
        fila y la segunda encuentra el trabajo hecho.
        """
        orden = await self.db.scalar(
            select(Order)
            .where(Order.id == orden_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if orden is None:
            raise RecursoNoEncontrado(ORDEN_NO_EXISTE)
        if orden.stripe_payment_intent_id is None:
            await self.db.commit()
            return orden

        intento = await self.pasarela.obtener(orden.stripe_payment_intent_id)
        self._verificar_que_corresponde(orden, intento)

        if orden.estado == PAGADA:
            await self.db.commit()
            if orden.capturada_en is None:
                await self._capturar(orden, intento)
            return orden

        if orden.estado != PENDIENTE:
            if intento.estado == COBRADO:
                # Cobrado por fuera (a mano en el panel de Stripe) sobre una
                # orden sin boletos: el dinero no se queda callado.
                await self._marcar_reembolso(orden)
            await self.db.commit()
            # Autorizo despues de que la orden se cerro: se suelta.
            if intento.estado == AUTORIZADO:
                await self.pasarela.cancelar(
                    intento.id, idempotency_key=f"cancelar-{orden.uuid}"
                )
            return orden

        if intento.estado == CANCELADO:
            await self._cerrar(orden, CANCELADA, "pago_cancelado")
            await self.db.commit()
            return orden

        if intento.estado not in (AUTORIZADO, COBRADO):
            # Todavia no paga, o la tarjeta fue rechazada y puede reintentar.
            await self.db.commit()
            return orden

        partidas = await self._partidas_vivas(orden.id)
        seat_ids = await self._seat_ids(partidas)
        vendidas = await self._vender(orden, partidas, seat_ids)
        if not vendidas:
            final = await self.pasarela.cancelar(
                intento.id, idempotency_key=f"cancelar-{orden.uuid}"
            )
            await self._cerrar(orden, EXPIRADA, "apartado_vencido")
            if final.estado == COBRADO:
                await self._marcar_reembolso(orden)
            await self.db.commit()
            return orden

        boletos = await self._emitir(orden, partidas)
        orden.estado = PAGADA
        orden.pagada_en = await self._ahora()
        await self._anunciar(orden, "orden.pagada", boletos)
        await self.db.commit()

        await self._capturar(orden, intento)
        # Las llaves de Redis ya no protegen nada: la butaca esta vendida
        # en Postgres. Se sueltan para no ocupar memoria hasta su TTL; si
        # Redis no responde, vencen solas.
        with contextlib.suppress(Exception):
            await self.holds.liberar(
                orden.performance_id, seat_ids, orden.hold_id or ""
            )
        return orden

    # ============================================================== privados

    async def _ahora(self) -> datetime:
        return await self.db.scalar(select(func.now()))

    @staticmethod
    def _verificar_que_corresponde(orden: Order, intento: IntentoDePago) -> None:
        if (
            intento.metadata.get("orden_uuid") != str(orden.uuid)
            or intento.monto_centavos != centavos(orden.total)
            or intento.moneda != orden.moneda
        ):
            raise IntentoNoCorresponde(
                "El pago no corresponde a la orden.",
                {"orden": str(orden.uuid), "intento": intento.id},
            )

    async def _reanudar_si_es_la_misma(
        self, previa: Order, ps_ids: list[int], email: str
    ) -> OrdenIniciada | None:
        """Doble clic o recarga: misma seleccion y mismo correo, misma orden.

        Si algo cambio, la previa se cancela, en Stripe primero. Si Stripe
        ya la autorizo, el comprador pago: no se le abre un segundo cobro.
        """
        partidas = await self._partidas_vivas(previa.id)
        correo = await self.db.scalar(
            select(Customer.email).where(Customer.id == previa.customer_id)
        )
        intento = (
            await self.pasarela.obtener(previa.stripe_payment_intent_id)
            if previa.stripe_payment_intent_id
            else None
        )
        misma = (
            sorted(p.performance_seat_id for p in partidas) == ps_ids
            and correo == email
        )
        if misma and intento is not None and intento.estado != CANCELADO:
            return OrdenIniciada(previa, partidas, intento.client_secret or "")

        if intento is not None and intento.estado != CANCELADO:
            # Stripe si deja cancelar una autorizacion, pero eso le
            # quitaria al comprador un pago que ya hizo en otra pestaña.
            # Esa orden se termina con `confirmar`, no se reemplaza.
            if intento.estado in (AUTORIZADO, COBRADO):
                raise PagoEnProceso(
                    "Ya hay un pago autorizado para estas butacas.",
                    {"orden": str(previa.uuid)},
                )
            intento = await self.pasarela.cancelar(
                intento.id, idempotency_key=f"cancelar-{previa.uuid}"
            )
            if intento.estado != CANCELADO:
                raise PagoEnProceso(
                    "Ya hay un pago en curso para estas butacas.",
                    {"orden": str(previa.uuid)},
                )
        await self._cerrar(previa, CANCELADA, "reemplazada")
        await self.db.flush()
        return None

    async def _extender(
        self,
        performance_id: int,
        seat_ids: list[int],
        hold_id: str,
        ahora: datetime,
    ) -> datetime:
        """Da tiempo para pagar, sin dejar que un apartado viva para siempre.

        El tope cuenta desde la primera butaca apartada: repetir el
        checkout no vuelve a dar quince minutos. Devuelve el vencimiento
        del espejo en Postgres, que es el de la orden.
        """
        inicio = await self.db.scalar(
            select(func.min(InventoryMove.creado_en)).where(
                InventoryMove.hold_id == hold_id,
                InventoryMove.estado_nuevo == APARTADO,
            )
        )
        limite = (inicio or ahora) + timedelta(
            seconds=settings.HOLD_TTL_SEGUNDOS + settings.HOLD_TTL_CHECKOUT_SEGUNDOS
        )
        ttl = min(
            settings.HOLD_TTL_CHECKOUT_SEGUNDOS, int((limite - ahora).total_seconds())
        )
        restante = min(
            [await self.holds.ttl_restante(performance_id, s) for s in seat_ids]
        )
        if ttl > restante and not await self.holds.extender(
            performance_id, seat_ids, hold_id, ttl=ttl
        ):
            raise ApartadoExpirado(
                "Tu apartado vencio. Elige tus butacas de nuevo.",
                {"performance_id": performance_id},
            )

        ttl_redis = min(
            [await self.holds.ttl_restante(performance_id, s) for s in seat_ids]
        )
        if ttl_redis <= MARGEN_ESPEJO_SEGUNDOS:
            raise ApartadoExpirado(
                "Tu apartado esta por vencer. Elige tus butacas de nuevo.",
                {"performance_id": performance_id},
            )
        vigencia = timedelta(seconds=ttl_redis - MARGEN_ESPEJO_SEGUNDOS)
        expira = await self.db.scalar(
            update(PerformanceSeat)
            .where(
                PerformanceSeat.performance_id == performance_id,
                PerformanceSeat.seat_id.in_(seat_ids),
                PerformanceSeat.hold_id == hold_id,
                PerformanceSeat.estado == APARTADO,
            )
            .values(hold_expira_en=func.now() + vigencia)
            .returning(PerformanceSeat.hold_expira_en)
            .execution_options(synchronize_session=False)
        )
        if expira is None:
            raise ApartadoExpirado(
                "Tu apartado vencio. Elige tus butacas de nuevo.",
                {"performance_id": performance_id},
            )
        return expira

    async def _cerrar_vencidas(self, ps_ids: list[int]) -> None:
        """Libera butacas de ordenes ajenas que nunca se pagaron.

        Sin esto, la partida viva de una orden abandonada le impediria a
        alguien mas comprar la butaca hasta que pasara un barrido. Si esa
        persona autoriza tarde, `finalizar` cancela su autorizacion.
        """
        vencidas = list(
            (
                await self.db.scalars(
                    update(Order)
                    .where(
                        Order.estado == PENDIENTE,
                        Order.expira_en <= func.now(),
                        Order.id.in_(
                            select(OrderItem.order_id).where(
                                OrderItem.performance_seat_id.in_(ps_ids),
                                OrderItem.anulado.is_(False),
                            )
                        ),
                    )
                    .values(estado=EXPIRADA, cerrada_motivo="apartado_vencido")
                    .returning(Order.id)
                    .execution_options(synchronize_session=False)
                )
            ).all()
        )
        if vencidas:
            await self._anular_partidas(vencidas)

    async def _detalle_de_butacas(self, ps_ids: list[int]) -> list[tuple]:
        return list(
            (
                await self.db.execute(
                    select(
                        PerformanceSeat.id,
                        PerformanceSeat.precio_final,
                        Zone.nombre,
                        Seat.fila,
                        Seat.numero,
                    )
                    .join(Seat, Seat.id == PerformanceSeat.seat_id)
                    .join(Zone, Zone.id == Seat.zone_id)
                    .where(PerformanceSeat.id.in_(ps_ids))
                    .order_by(Zone.nombre, Seat.fila, PerformanceSeat.id)
                )
            ).all()
        )

    async def _comprador(
        self, email: str, datos: DatosDelComprador, telefono: str | None
    ) -> Customer:
        """Un correo, un comprador.

        De uno que ya existe solo se llenan los datos que faltan: cualquiera
        puede escribir un correo ajeno en un checkout, y eso no le da
        derecho a cambiarle el telefono a su dueño.
        """
        await self.db.execute(
            pg_insert(Customer)
            .values(
                email=email,
                nombre=datos.nombre.strip() or None,
                apellidos=(datos.apellidos or "").strip() or None,
                telefono=telefono,
            )
            .on_conflict_do_nothing(index_elements=[Customer.email])
        )
        persona = await self.db.scalar(
            select(Customer)
            .where(Customer.email == email)
            .execution_options(populate_existing=True)
        )
        if persona.nombre is None:
            persona.nombre = datos.nombre.strip() or None
        if persona.apellidos is None:
            persona.apellidos = (datos.apellidos or "").strip() or None
        if persona.telefono is None:
            persona.telefono = telefono
        await self.db.flush()
        return persona

    async def _partidas_vivas(self, orden_id: int) -> list[OrderItem]:
        return list(
            (
                await self.db.scalars(
                    select(OrderItem)
                    .where(OrderItem.order_id == orden_id, OrderItem.anulado.is_(False))
                    .order_by(OrderItem.id)
                )
            ).all()
        )

    async def _seat_ids(self, partidas: list[OrderItem]) -> list[int]:
        return list(
            (
                await self.db.scalars(
                    select(PerformanceSeat.seat_id).where(
                        PerformanceSeat.id.in_(
                            [p.performance_seat_id for p in partidas]
                        )
                    )
                )
            ).all()
        )

    async def _vender(
        self, orden: Order, partidas: list[OrderItem], seat_ids: list[int]
    ) -> bool:
        """Capa 2: todas las butacas pasan a vendido, o ninguna.

        Redis dice si el apartado sigue siendo nuestro; el UPDATE lo
        confirma en Postgres con el mismo dueño. Lo que no cumpla las dos
        cosas no se vende.
        """
        if not partidas or not orden.hold_id:
            return False
        if not await self.holds.es_titular(
            orden.performance_id, seat_ids, orden.hold_id
        ):
            return False

        ps_ids = [p.performance_seat_id for p in partidas]
        try:
            async with self.db.begin_nested():
                vendidas = list(
                    (
                        await self.db.scalars(
                            update(PerformanceSeat)
                            .where(
                                PerformanceSeat.id.in_(ps_ids),
                                PerformanceSeat.estado == APARTADO,
                                PerformanceSeat.hold_id == orden.hold_id,
                            )
                            .values(
                                estado=VENDIDO,
                                hold_id=None,
                                hold_expira_en=None,
                                version=PerformanceSeat.version + 1,
                            )
                            .returning(PerformanceSeat.id)
                            .execution_options(synchronize_session=False)
                        )
                    ).all()
                )
                if len(vendidas) != len(ps_ids):
                    raise _VentaIncompleta
        except _VentaIncompleta:
            return False

        await self.db.execute(
            insert(InventoryMove).values(
                [
                    {
                        "performance_seat_id": ps_id,
                        "estado_previo": APARTADO,
                        "estado_nuevo": VENDIDO,
                        "motivo": "vendido",
                        "actor_tipo": "customer",
                        "actor_id": orden.customer_id,
                        "order_id": orden.id,
                        "hold_id": orden.hold_id,
                    }
                    for ps_id in ps_ids
                ]
            )
        )
        return True

    async def _emitir(self, orden: Order, partidas: list[OrderItem]) -> list[Ticket]:
        boletos = [
            Ticket(
                order_item_id=p.id,
                holder_customer_id=orden.customer_id,
                codigo=nuevo_codigo_de_boleto(),
                semilla=secrets.token_bytes(32),
                estado=EstadoBoleto.VALIDO.value,
            )
            for p in partidas
        ]
        self.db.add_all(boletos)
        await self.db.flush()
        return boletos

    async def _capturar(self, orden: Order, intento: IntentoDePago) -> None:
        """Cobra. Solo un rechazo definitivo deshace la venta.

        `PasarelaNoDisponible` sube tal cual: la orden queda pagada sin
        capturar y el siguiente intento (Stripe reenvia el webhook) la
        captura con la misma llave de idempotencia.
        """
        if intento.estado != COBRADO:
            try:
                await self.pasarela.capturar(
                    intento.id, idempotency_key=f"capturar-{orden.uuid}"
                )
            except CobroRechazado:
                await self._deshacer_venta(orden)
                await self.db.commit()
                return
        await self.db.execute(
            update(Order)
            .where(Order.id == orden.id, Order.capturada_en.is_(None))
            .values(capturada_en=func.now())
        )
        await self.db.commit()
        await self.db.refresh(orden)

    async def _deshacer_venta(self, orden: Order) -> None:
        """Stripe no dejo cobrar: los boletos no valen y las butacas vuelven."""
        orden = await self.db.scalar(
            select(Order)
            .where(Order.id == orden.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        partidas = await self._partidas_vivas(orden.id)
        ids = [p.id for p in partidas]
        ps_ids = [p.performance_seat_id for p in partidas]
        await self.db.execute(
            update(Ticket)
            .where(Ticket.order_item_id.in_(ids))
            .values(estado=EstadoBoleto.CANCELADO.value)
        )
        devueltas = list(
            (
                await self.db.scalars(
                    update(PerformanceSeat)
                    .where(
                        PerformanceSeat.id.in_(ps_ids),
                        PerformanceSeat.estado == VENDIDO,
                    )
                    .values(estado=DISPONIBLE, version=PerformanceSeat.version + 1)
                    .returning(PerformanceSeat.id)
                    .execution_options(synchronize_session=False)
                )
            ).all()
        )
        if devueltas:
            await self.db.execute(
                insert(InventoryMove).values(
                    [
                        {
                            "performance_seat_id": ps_id,
                            "estado_previo": VENDIDO,
                            "estado_nuevo": DISPONIBLE,
                            "motivo": "cobro rechazado",
                            "order_id": orden.id,
                        }
                        for ps_id in devueltas
                    ]
                )
            )
        await self._cerrar(orden, CANCELADA, "captura_rechazada")
        await self._anunciar(orden, "orden.cancelada", [])

    async def _marcar_reembolso(self, orden: Order) -> None:
        """Hay un cobro sin boletos. Lo ve operacion en la bandeja de salida.

        El reembolso todavia no es automatico: se deja marcado una sola
        vez, para que nadie lo pase por alto ni se avise dos veces.
        """
        if orden.cerrada_motivo == REQUIERE_REEMBOLSO:
            return
        if orden.estado == PENDIENTE:
            await self._cerrar(orden, CANCELADA, REQUIERE_REEMBOLSO)
        else:
            orden.cerrada_motivo = REQUIERE_REEMBOLSO
        await self._anunciar(orden, "orden.requiere_reembolso", [])

    async def _cerrar(self, orden: Order, estado: str, motivo: str) -> None:
        orden.estado = estado
        orden.cerrada_motivo = motivo
        await self.db.flush()
        await self._anular_partidas([orden.id])

    async def _anular_partidas(self, orden_ids: list[int]) -> None:
        await self.db.execute(
            update(OrderItem)
            .where(OrderItem.order_id.in_(orden_ids), OrderItem.anulado.is_(False))
            .values(anulado=True)
            .execution_options(synchronize_session=False)
        )

    async def _anunciar(self, orden: Order, tipo: str, boletos: list[Ticket]) -> None:
        """Escribe el hecho en la bandeja de salida, en esta transaccion."""
        cliente_uuid, evento_uuid, funcion_uuid, comprador_uuid = (
            await self.db.execute(
                select(Client.uuid, Event.uuid, Performance.uuid, Customer.uuid)
                .select_from(Performance)
                .join(Event, Event.id == Performance.event_id)
                .join(Client, Client.id == Event.client_id)
                .join(Customer, Customer.id == orden.customer_id)
                .where(Performance.id == orden.performance_id)
            )
        ).one()
        self.db.add(
            OutboxEvent(
                tipo=tipo,
                agregado_uuid=orden.uuid,
                payload={
                    "orden_uuid": str(orden.uuid),
                    "folio": orden.folio,
                    "estado": orden.estado,
                    "cliente_uuid": str(cliente_uuid),
                    "evento_uuid": str(evento_uuid),
                    "funcion_uuid": str(funcion_uuid),
                    "comprador_uuid": str(comprador_uuid),
                    "total": str(orden.total),
                    "moneda": orden.moneda,
                    "boletos": [str(b.uuid) for b in boletos],
                },
            )
        )
        await self.db.flush()


class _VentaIncompleta(Exception):
    """Alguna butaca no paso a vendido: se revierte el savepoint."""
