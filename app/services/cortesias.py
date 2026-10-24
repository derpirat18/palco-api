"""Cortesias (C1).

Las otorga el organizador (o PSD) sobre butacas concretas de una funcion,
a nombre de una persona con su correo. Reglas:

- **Consumen el mismo inventario.** La butaca pasa a `cortesia` con el
  mismo `UPDATE` condicional que usa el panel, y su partida entra al indice
  unico de `order_items` (capa 3): ni una cortesia ni una venta pueden
  quedarse con una butaca que ya es de otro.
- Se toman de butacas `disponible`, `bloqueado` (lo usual: el organizador
  reserva lugares para invitados) o con un apartado ya vencido. Nunca de
  vendidas, cortesias, apartados vigentes ni kill.
- Se pueden dar antes de que abra la venta, pero no en funciones
  canceladas, ya realizadas o sin inventario.
- Son una orden normal con total $0, `canal = 'cortesia'`, ya pagada y ya
  "capturada": no hay nada que cobrar y el barrido no debe intentarlo.
- Sus boletos son boletos de verdad, con QR, y llegan por correo con un
  texto propio (sin "tu compra" ni resumen de pago).
- Revocar devuelve cada butaca al estado de donde salio y cancela los
  boletos, salvo que alguno ya haya entrado.
"""

import secrets
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import FUNCION_NO_EXISTE, ErrorDeDominio, RecursoNoEncontrado
from app.core.telefonos import normalizar_telefono
from app.models.base import EstadoAsiento, EstadoBoleto, EstadoFuncion, EstadoOrden
from app.models.catalog import Event, Performance, Seat, Zone
from app.models.inventory import InventoryMove, PerformanceSeat
from app.models.outbox import OutboxEvent
from app.models.sales import Cortesia, Customer, Order, OrderItem, Ticket
from app.services.checkout import CERO, nuevo_codigo_de_boleto, nuevo_folio
from app.services.venta import iva_de

CANAL = "cortesia"
MAX_POR_CORTESIA = 20
CORTESIA_NO_EXISTE = "No encontramos esta cortesía."

# De donde se puede tomar una cortesia. Un apartado vencido tambien.
_ORIGENES = (EstadoAsiento.DISPONIBLE.value, EstadoAsiento.BLOQUEADO.value)
_FUNCION_CERRADA = (EstadoFuncion.CANCELADA.value, EstadoFuncion.REALIZADA.value)


class CortesiaInvalida(ErrorDeDominio):
    codigo = "cortesia_invalida"
    http_status = 409


@dataclass(frozen=True)
class CortesiaEmitida:
    orden: Order
    cortesia: Cortesia
    comprador: Customer
    partidas: list[OrderItem]


def _tomable(ps: PerformanceSeat, ahora) -> bool:
    if ps.estado in _ORIGENES:
        return True
    return (
        ps.estado == EstadoAsiento.APARTADO.value
        and ps.hold_expira_en is not None
        and ps.hold_expira_en <= ahora
    )


class CortesiaService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def otorgar(
        self,
        performance_id: int,
        seat_ids: list[int],
        email: str,
        nombre: str,
        motivo: str,
        actor_id: int | None,
        telefono: str | None = None,
    ) -> CortesiaEmitida:
        # Un telefono mal escrito se rechaza antes de tocar ningun lugar.
        telefono = normalizar_telefono(telefono)
        if not seat_ids:
            raise CortesiaInvalida("Elige al menos un lugar.")
        if len(seat_ids) > MAX_POR_CORTESIA:
            raise CortesiaInvalida(
                f"Una cortesía puede tener hasta {MAX_POR_CORTESIA} lugares.",
                {"solicitadas": len(seat_ids)},
            )
        if len(set(seat_ids)) != len(seat_ids):
            raise CortesiaInvalida("Hay lugares repetidos en la solicitud.")

        funcion = await self.db.get(Performance, performance_id)
        if funcion is None:
            raise RecursoNoEncontrado(FUNCION_NO_EXISTE)
        if funcion.estado in _FUNCION_CERRADA:
            raise CortesiaInvalida(
                "Esta función ya no admite cortesías: está cancelada o ya se realizó.",
                {"estado": funcion.estado},
            )
        if not funcion.inventario_generado:
            raise CortesiaInvalida(
                "Genera el inventario de esta función antes de dar cortesías."
            )
        evento = await self.db.get(Event, funcion.event_id)

        ahora = await self.db.scalar(select(func.now()))
        filas = (
            await self.db.execute(
                select(PerformanceSeat, Seat, Zone)
                .join(Seat, Seat.id == PerformanceSeat.seat_id)
                .join(Zone, Zone.id == Seat.zone_id)
                .where(
                    PerformanceSeat.performance_id == performance_id,
                    PerformanceSeat.seat_id.in_(seat_ids),
                )
                .order_by(Zone.orden, Seat.fila, Seat.id)
            )
        ).all()
        encontradas = {s.id for _, s, _ in filas}
        ajenas = sorted(set(seat_ids) - encontradas)
        if ajenas:
            raise CortesiaInvalida(
                "Hay lugares que no pertenecen a esta función.", {"seat_ids": ajenas}
            )
        ocupadas = sorted(s.id for ps, s, _ in filas if not _tomable(ps, ahora))
        if ocupadas:
            raise CortesiaInvalida(
                "Algunos lugares ya están vendidos, apartados, son cortesía o no "
                "existen en el mapa. Elige otros.",
                {"seat_ids": ocupadas},
            )

        previos = {ps.id: ps.estado for ps, _, _ in filas}
        async with self.db.begin_nested():
            # Mismas reglas que arriba, repetidas en la base: entre la lectura
            # y la escritura un comprador pudo apartar alguna.
            tomadas = set(
                (
                    await self.db.scalars(
                        update(PerformanceSeat)
                        .where(
                            PerformanceSeat.id.in_(list(previos)),
                            or_(
                                PerformanceSeat.estado.in_(_ORIGENES),
                                and_(
                                    PerformanceSeat.estado
                                    == EstadoAsiento.APARTADO.value,
                                    PerformanceSeat.hold_expira_en <= func.now(),
                                ),
                            ),
                        )
                        .values(
                            estado=EstadoAsiento.CORTESIA.value,
                            hold_id=None,
                            hold_expira_en=None,
                            version=PerformanceSeat.version + 1,
                        )
                        .returning(PerformanceSeat.id)
                        .execution_options(synchronize_session="fetch")
                    )
                ).all()
            )
            if tomadas != set(previos):
                raise CortesiaInvalida(
                    "Algunos lugares cambiaron mientras se procesaba la cortesía. "
                    "Actualiza el mapa e intenta de nuevo."
                )

            comprador = await self._persona(email, nombre)
            iva = iva_de(evento)
            orden = Order(
                folio=nuevo_folio(),
                customer_id=comprador.id,
                performance_id=performance_id,
                estado=EstadoOrden.PAGADA.value,
                subtotal=CERO,
                iva=CERO,
                descuento=CERO,
                total=CERO,
                comision_pct_snap=CERO,
                iva_pct_snap=iva.pct,
                idempotency_key=f"cortesia-{secrets.token_hex(16)}",
                pagada_en=func.now(),
                # Nada que capturar: sin esto el barrido intentaria cobrarla.
                capturada_en=func.now(),
                canal=CANAL,
                nombre_snap=nombre.strip()[:120] or None,
                # Solo lo que escribio quien la dio (OB1); el telefono, si lo puso.
                email_snap=email.strip().lower(),
                telefono_snap=telefono,
            )
            self.db.add(orden)
            await self.db.flush()

            partidas = [
                OrderItem(
                    order_id=orden.id,
                    performance_seat_id=ps.id,
                    zona_snap=zona.nombre,
                    asiento_snap=f"Fila {seat.fila} · {seat.numero}",
                    precio_unitario=CERO,
                    iva=CERO,
                    descuento=CERO,
                    importe=CERO,
                )
                for ps, seat, zona in filas
            ]
            self.db.add_all(partidas)
            await self.db.flush()

            boletos = [
                Ticket(
                    order_item_id=p.id,
                    holder_customer_id=comprador.id,
                    codigo=nuevo_codigo_de_boleto(),
                    semilla=secrets.token_bytes(32),
                    estado=EstadoBoleto.VALIDO.value,
                )
                for p in partidas
            ]
            self.db.add_all(boletos)
            cortesia = Cortesia(
                order_id=orden.id, motivo=motivo.strip(), otorgada_por=actor_id
            )
            self.db.add(cortesia)
            self.db.add_all(
                InventoryMove(
                    performance_seat_id=ps_id,
                    estado_previo=previo,
                    estado_nuevo=EstadoAsiento.CORTESIA.value,
                    motivo="cortesia",
                    actor_tipo="user" if actor_id else None,
                    actor_id=actor_id,
                    order_id=orden.id,
                )
                for ps_id, previo in previos.items()
            )
            await self.db.flush()
            # Sin datos personales en la bandeja: el correo se arma desde la base.
            self.db.add(
                OutboxEvent(
                    tipo="cortesia.emitida",
                    agregado_uuid=orden.uuid,
                    payload={
                        "orden_uuid": str(orden.uuid),
                        "folio": orden.folio,
                        "funcion_uuid": str(funcion.uuid),
                        "boletos": [str(b.uuid) for b in boletos],
                    },
                )
            )
            await self.db.flush()
        await self.db.refresh(orden)
        return CortesiaEmitida(orden, cortesia, comprador, partidas)

    async def _persona(self, email: str, nombre: str) -> Customer:
        """Un correo, una persona. A una que ya existe no se le cambia nada."""
        correo = email.strip().lower()
        await self.db.execute(
            pg_insert(Customer)
            .values(email=correo, nombre=nombre.strip() or None)
            .on_conflict_do_nothing(index_elements=[Customer.email])
        )
        persona = await self.db.scalar(select(Customer).where(Customer.email == correo))
        if persona.nombre is None:
            persona.nombre = nombre.strip() or None
            await self.db.flush()
        return persona

    async def listar(self, performance_id: int) -> list[tuple]:
        """(orden, cortesia, persona, partidas) de cada cortesia de la funcion."""
        filas = (
            await self.db.execute(
                select(Order, Cortesia, Customer)
                .join(Cortesia, Cortesia.order_id == Order.id)
                .join(Customer, Customer.id == Order.customer_id)
                .where(Order.performance_id == performance_id, Order.canal == CANAL)
                .order_by(Cortesia.otorgada_en.desc(), Order.id.desc())
            )
        ).all()
        resultado = []
        for orden, cortesia, persona in filas:
            partidas = list(
                (
                    await self.db.scalars(
                        select(OrderItem)
                        .where(OrderItem.order_id == orden.id)
                        .order_by(OrderItem.id)
                    )
                ).all()
            )
            resultado.append((orden, cortesia, persona, partidas))
        return resultado

    async def obtener(self, orden_uuid: UUID) -> tuple[Order, Cortesia]:
        fila = (
            await self.db.execute(
                select(Order, Cortesia)
                .join(Cortesia, Cortesia.order_id == Order.id)
                .where(Order.uuid == orden_uuid, Order.canal == CANAL)
            )
        ).first()
        if fila is None:
            raise RecursoNoEncontrado(CORTESIA_NO_EXISTE)
        return fila[0], fila[1]

    async def revocar(self, orden_uuid: UUID, actor_id: int | None) -> Order:
        """Devuelve los lugares a su estado previo y cancela los boletos."""
        orden, cortesia = await self.obtener(orden_uuid)
        orden = await self.db.scalar(
            select(Order).where(Order.id == orden.id).with_for_update()
        )
        if orden.estado == EstadoOrden.CANCELADA.value:
            return orden  # Ya revocada: repetirlo no hace nada.

        filas = (
            await self.db.execute(
                select(OrderItem, Ticket)
                .join(Ticket, Ticket.order_item_id == OrderItem.id)
                .where(OrderItem.order_id == orden.id, OrderItem.anulado.is_(False))
            )
        ).all()
        if any(t.estado == EstadoBoleto.USADO.value for _, t in filas):
            raise CortesiaInvalida(
                "No se puede revocar: al menos un boleto de esta cortesía ya entró."
            )

        # Cada butaca vuelve a donde estaba antes de la cortesia.
        previos = dict(
            (
                await self.db.execute(
                    select(
                        InventoryMove.performance_seat_id, InventoryMove.estado_previo
                    ).where(
                        InventoryMove.order_id == orden.id,
                        InventoryMove.estado_nuevo == EstadoAsiento.CORTESIA.value,
                    )
                )
            ).all()
        )
        async with self.db.begin_nested():
            for partida, boleto in filas:
                destino = previos.get(partida.performance_seat_id)
                if destino not in _ORIGENES:
                    # Un apartado vencido no se restaura: queda libre.
                    destino = EstadoAsiento.DISPONIBLE.value
                await self.db.execute(
                    update(PerformanceSeat)
                    .where(
                        PerformanceSeat.id == partida.performance_seat_id,
                        PerformanceSeat.estado == EstadoAsiento.CORTESIA.value,
                    )
                    .values(estado=destino, version=PerformanceSeat.version + 1)
                )
                partida.anulado = True
                boleto.estado = EstadoBoleto.CANCELADO.value
                self.db.add(
                    InventoryMove(
                        performance_seat_id=partida.performance_seat_id,
                        estado_previo=EstadoAsiento.CORTESIA.value,
                        estado_nuevo=destino,
                        motivo="cortesia_revocada",
                        actor_tipo="user" if actor_id else None,
                        actor_id=actor_id,
                        order_id=orden.id,
                    )
                )
            orden.estado = EstadoOrden.CANCELADA.value
            orden.cerrada_motivo = "cortesia_revocada"
            cortesia.revocada_por = actor_id
            cortesia.revocada_en = func.now()
            self.db.add(
                OutboxEvent(
                    tipo="cortesia.revocada",
                    agregado_uuid=orden.uuid,
                    payload={"orden_uuid": str(orden.uuid), "folio": orden.folio},
                )
            )
            await self.db.flush()
        await self.db.refresh(orden)
        return orden
