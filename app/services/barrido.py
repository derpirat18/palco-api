"""Barrido: lo que nadie mas va a terminar si no se hace aqui.

El flujo normal se limpia solo mientras hay trafico: el siguiente
comprador cierra la orden abandonada que le estorba, y Stripe reintenta
el webhook de una captura que fallo. Pero sin trafico y sin reintentos,
quedan cosas a medias que cuestan dinero:

1. Butacas que el mapa pinta como apartadas aunque su apartado ya vencio.
2. Ordenes pendientes vencidas, con su PaymentIntent vivo en Stripe.
3. Ordenes pagadas con boletos emitidos cuya captura no se hizo. Stripe
   suelta la autorizacion a los 7 dias: despues de eso, boletos sin cobro.
4. De esas, las que ya pasaron 5 dias: alguien tiene que verlas hoy.

Cada tarea usa su propia transaccion y cada orden la suya: un error en
una no detiene a las demas, se cuenta y se reintenta en la siguiente
vuelta. Todo es idempotente: correr dos barridos seguidos no hace nada
la segunda vez.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import exists, func, insert, select, tuple_, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.base import EstadoAsiento, EstadoOrden
from app.models.inventory import InventoryMove, PerformanceSeat
from app.models.outbox import OutboxEvent
from app.models.sales import Order
from app.services.checkout import CheckoutService
from app.services.holds import HoldService
from app.services.pasarela import Pasarela

log = logging.getLogger("palco.barrido")

# Margen sobre el vencimiento antes de tocar algo. Un comprador que dio
# clic en "pagar" justo al limite merece unos segundos.
GRACIA = timedelta(seconds=60)
# Una pagada sin capturar recien emitida la esta capturando el flujo
# normal; el barrido no se le cruza.
ESPERA_PARA_REINTENTAR_CAPTURA = timedelta(minutes=2)
# Stripe libera la autorizacion de tarjeta a los 7 dias.
CAPTURA_ATRASADA = timedelta(days=5)

APARTADO = EstadoAsiento.APARTADO.value
DISPONIBLE = EstadoAsiento.DISPONIBLE.value
PENDIENTE = EstadoOrden.PENDIENTE.value
PAGADA = EstadoOrden.PAGADA.value

Sesiones = Callable[[], AbstractAsyncContextManager[AsyncSession]]


@dataclass
class ResultadoDelBarrido:
    apartados_soltados: int = 0
    ordenes_cerradas: int = 0
    ordenes_vendidas_tarde: int = 0
    capturas_hechas: int = 0
    capturas_atrasadas: int = 0
    errores: int = 0

    def hubo_algo(self) -> bool:
        return any(vars(self).values())


class BarridoService:
    def __init__(
        self,
        sesiones: Sesiones,
        holds: HoldService,
        pasarela: Pasarela,
        lote: int = 200,
    ) -> None:
        self.sesiones = sesiones
        self.holds = holds
        self.pasarela = pasarela
        self.lote = lote

    async def una_vuelta(self) -> ResultadoDelBarrido:
        r = ResultadoDelBarrido()
        # Primero las ordenes: si el comprador alcanzo a pagar, su butaca
        # tiene que seguir apartada cuando `finalizar` la busque.
        await self._cerrar_ordenes_vencidas(r)
        await self._soltar_apartados_vencidos(r)
        await self._reintentar_capturas(r)
        await self._alertar_capturas_atrasadas(r)
        return r

    # ---------------------------------------------------------------- 1

    async def _soltar_apartados_vencidos(self, r: ResultadoDelBarrido) -> None:
        """Devuelve a disponible lo que Postgres y Redis ya dan por vencido.

        Redis manda: si la llave sigue siendo de ese apartado (un checkout
        lo extendio en Redis y su transaccion se revirtio, por ejemplo),
        la butaca no se toca aunque Postgres diga que vencio.
        """
        try:
            async with self.sesiones() as db:
                filas = (
                    await db.execute(
                        select(
                            PerformanceSeat.id,
                            PerformanceSeat.performance_id,
                            PerformanceSeat.seat_id,
                            PerformanceSeat.hold_id,
                        )
                        .where(
                            PerformanceSeat.estado == APARTADO,
                            PerformanceSeat.hold_expira_en < func.now() - GRACIA,
                        )
                        .order_by(PerformanceSeat.hold_expira_en)
                        .limit(self.lote)
                    )
                ).all()
                vencidas = [
                    (ps_id, hold_id)
                    for ps_id, pid, seat_id, hold_id in filas
                    if not await self.holds.es_titular(pid, [seat_id], hold_id or "")
                ]
                if not vencidas:
                    return

                soltadas = (
                    await db.execute(
                        update(PerformanceSeat)
                        .where(
                            tuple_(PerformanceSeat.id, PerformanceSeat.hold_id).in_(
                                vencidas
                            ),
                            PerformanceSeat.estado == APARTADO,
                            PerformanceSeat.hold_expira_en < func.now() - GRACIA,
                        )
                        .values(
                            estado=DISPONIBLE,
                            hold_id=None,
                            hold_expira_en=None,
                            version=PerformanceSeat.version + 1,
                        )
                        .returning(PerformanceSeat.id)
                        .execution_options(synchronize_session=False)
                    )
                ).all()
                dueno = dict(vencidas)
                if soltadas:
                    await db.execute(
                        insert(InventoryMove).values(
                            [
                                {
                                    "performance_seat_id": ps_id,
                                    "estado_previo": APARTADO,
                                    "estado_nuevo": DISPONIBLE,
                                    "motivo": "apartado vencido",
                                    "hold_id": dueno[ps_id],
                                }
                                for (ps_id,) in soltadas
                            ]
                        )
                    )
                await db.commit()
                r.apartados_soltados += len(soltadas)
        except Exception:
            log.exception("Barrido: no se pudieron soltar apartados vencidos")
            r.errores += 1

    # ---------------------------------------------------------------- 2

    async def _cerrar_ordenes_vencidas(self, r: ResultadoDelBarrido) -> None:
        ids = await self._ids(
            select(Order.id)
            .where(Order.estado == PENDIENTE, Order.expira_en < func.now() - GRACIA)
            .order_by(Order.expira_en)
        )
        for orden_id in ids:
            try:
                async with self.sesiones() as db:
                    orden = await CheckoutService(
                        db, self.holds, self.pasarela
                    ).expirar(orden_id)
                if orden is None:
                    continue
                if orden.estado == PAGADA:
                    r.ordenes_vendidas_tarde += 1
                elif orden.estado != PENDIENTE:
                    r.ordenes_cerradas += 1
            except Exception:
                log.exception("Barrido: no se pudo cerrar la orden %s", orden_id)
                r.errores += 1

    # ---------------------------------------------------------------- 3

    async def _reintentar_capturas(self, r: ResultadoDelBarrido) -> None:
        ids = await self._ids(
            select(Order.id)
            .where(
                Order.estado == PAGADA,
                Order.capturada_en.is_(None),
                Order.pagada_en < func.now() - ESPERA_PARA_REINTENTAR_CAPTURA,
            )
            .order_by(Order.pagada_en)
        )
        for orden_id in ids:
            try:
                async with self.sesiones() as db:
                    orden = await CheckoutService(
                        db, self.holds, self.pasarela
                    ).finalizar(orden_id)
                if orden.capturada_en is not None:
                    r.capturas_hechas += 1
            except Exception:
                log.exception("Barrido: no se pudo capturar la orden %s", orden_id)
                r.errores += 1

    # ---------------------------------------------------------------- 4

    async def _alertar_capturas_atrasadas(self, r: ResultadoDelBarrido) -> None:
        """Una sola vez por orden: el aviso es para que alguien actue hoy."""
        tipo = "orden.captura_atrasada"
        try:
            async with self.sesiones() as db:
                atrasadas = (
                    await db.execute(
                        select(Order.uuid, Order.folio, Order.total, Order.moneda)
                        .where(
                            Order.estado == PAGADA,
                            Order.capturada_en.is_(None),
                            Order.pagada_en < func.now() - CAPTURA_ATRASADA,
                            ~exists().where(
                                OutboxEvent.agregado_uuid == Order.uuid,
                                OutboxEvent.tipo == tipo,
                            ),
                        )
                        .limit(self.lote)
                    )
                ).all()
                for orden_uuid, folio, total, moneda in atrasadas:
                    log.error(
                        "Orden %s pagada sin capturar desde hace mas de %s dias: "
                        "Stripe libera la autorizacion a los 7.",
                        folio,
                        CAPTURA_ATRASADA.days,
                    )
                    db.add(
                        OutboxEvent(
                            tipo=tipo,
                            agregado_uuid=orden_uuid,
                            payload={
                                "orden_uuid": str(orden_uuid),
                                "folio": folio,
                                "total": str(total),
                                "moneda": moneda,
                            },
                        )
                    )
                await db.commit()
                r.capturas_atrasadas += len(atrasadas)
        except Exception:
            log.exception("Barrido: no se pudieron revisar capturas atrasadas")
            r.errores += 1

    # ---------------------------------------------------------- privados

    async def _ids(self, consulta) -> list[int]:
        async with self.sesiones() as db:
            return list((await db.scalars(consulta.limit(self.lote))).all())
