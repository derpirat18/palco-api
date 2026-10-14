"""Apartado de butacas desde la API: Postgres alrededor de Redis.

`holds.py` aparta en Redis, pero Redis solo sabe si una llave existe. No
sabe que una butaca esta vendida, en kill o bloqueada: esas no tienen
llave, y Redis las apartaria sin chistar. La capa 2 lo frenaria al
cobrar, pero para entonces el comprador ya paso por Stripe.

Por eso este servicio pone a Postgres antes y despues de Redis:

  1. Antes: la funcion esta a la venta y cada butaca es apartable segun
     `performance_seats`. Una butaca vendida nunca llega a Redis.
  2. Redis aparta, todo o nada. Es quien resuelve la carrera.
  3. Despues: el apartado se refleja en `performance_seats` con un
     `UPDATE` condicional. Ese espejo es el que la capa 2 revisa al
     cobrar, y el que pinta el mapa.

Invariante del espejo: el vencimiento en Postgres nunca es posterior al
de Redis. Se escribe como el menor TTL real de Redis menos un margen. Al
reves, Postgres seguiria diciendo "apartada" cuando Redis ya la solto, y
nadie podria comprarla hasta que Postgres la diera por vencida. Asi, en
la duda Postgres deja pasar y Redis decide.

El `hold_id` es la credencial del apartado: un UUID4 que genera el front
antes del primer clic. Por eso un reintento llega con el mismo id y no
le dice al comprador que alguien mas aparto su propia butaca.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import and_, func, insert, or_, select, tuple_, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import (
    FUNCION_NO_EXISTE,
    AsientoNoDisponible,
    DejaButacaSuelta,
    FuncionNoALaVenta,
    LimiteDeAsientos,
    RecursoNoEncontrado,
)
from app.models.base import EstadoAsiento
from app.models.catalog import Client, Event, Performance, Seat
from app.models.inventory import InventoryMove, PerformanceSeat
from app.services.holds import HoldService
from app.services.inventory import ConflictoDeInventario
from app.services.sueltas import Lugar, butaca_suelta
from app.services.venta import motivo_no_se_vende, visible_al_publico

# Cuanto antes que Redis vence el apartado en Postgres. Cubre la latencia
# entre el SET en Redis y el UPDATE en Postgres, y el reloj de cada uno.
MARGEN_ESPEJO_SEGUNDOS = 5

APARTADO = EstadoAsiento.APARTADO.value
DISPONIBLE = EstadoAsiento.DISPONIBLE.value


def _libre_para(
    estado: str,
    dueno: str | None,
    expira: datetime | None,
    hold_id: str,
    ahora: datetime,
) -> bool:
    """Si esta persona puede quedarse con la butaca.

    Disponible, apartada por ella misma, o apartada por alguien mas pero
    ya vencida en Postgres. Es la misma condicion que repite el `UPDATE`.
    """
    if estado == DISPONIBLE:
        return True
    if estado == APARTADO:
        if dueno == hold_id:
            return True
        return expira is not None and expira <= ahora
    return False


@dataclass(frozen=True)
class ButacaApartada:
    seat_id: int
    performance_seat_id: int
    precio: Decimal


@dataclass(frozen=True)
class EstadoDelApartado:
    hold_id: str
    performance_id: int
    butacas: tuple[ButacaApartada, ...]
    expira_en: datetime
    segundos_restantes: int


class ApartadoService:
    def __init__(self, db: AsyncSession, holds: HoldService) -> None:
        self.db = db
        self.holds = holds

    async def apartar(
        self, performance_id: int, seat_ids: Sequence[int], hold_id: str
    ) -> EstadoDelApartado:
        """Aparta todas las butacas o ninguna, y lo deja en Postgres.

        Repetir la misma peticion con el mismo `hold_id` no falla ni
        alarga el apartado: es un reintento, no una compra nueva.
        """
        seat_ids = list(seat_ids)
        if not seat_ids:
            raise LimiteDeAsientos("No se recibio ninguna butaca.")
        if len(set(seat_ids)) != len(seat_ids):
            raise LimiteDeAsientos("Hay butacas repetidas en la solicitud.")

        ahora = await self._ahora()
        await self._funcion_a_la_venta(performance_id, ahora)

        filas = {
            f.seat_id: f
            for f in (
                await self.db.scalars(
                    select(PerformanceSeat).where(
                        PerformanceSeat.performance_id == performance_id,
                        PerformanceSeat.seat_id.in_(seat_ids),
                    )
                )
            ).all()
        }
        faltantes = [s for s in seat_ids if s not in filas]
        if faltantes:
            raise ConflictoDeInventario(
                "Hay butacas que no pertenecen a esta funcion.",
                {"butacas": faltantes},
            )

        for seat_id in seat_ids:
            if not self._apartable(filas[seat_id], hold_id, ahora):
                raise AsientoNoDisponible(
                    "Esa butaca ya no esta disponible.",
                    {"seat_id": seat_id, "performance_id": performance_id},
                )

        await self._verificar_tope(performance_id, seat_ids, hold_id, ahora)

        suelta = await self._butaca_que_quedaria_suelta(
            performance_id, seat_ids, hold_id, ahora
        )
        if suelta is not None:
            raise DejaButacaSuelta(
                "Esa seleccion deja una butaca sola. Elige butacas juntas o "
                "deja al menos dos libres al lado.",
                {"seat_id": suelta, "performance_id": performance_id},
            )

        # Las que ya eran suyas antes de esta peticion. Si algo falla
        # despues de Redis, solo se sueltan las demas: un error al agregar
        # una butaca no puede costarle al comprador las que ya tenia.
        ya_suyas = {
            s for s, f in filas.items() if f.estado == APARTADO and f.hold_id == hold_id
        }

        await self.holds.apartar(
            performance_id, seat_ids, hold_id=hold_id, ttl=settings.HOLD_TTL_SEGUNDOS
        )

        try:
            await self._reflejar(performance_id, seat_ids, hold_id, ya_suyas, filas)
        except Exception:
            nuevas = [s for s in seat_ids if s not in ya_suyas]
            await self.holds.liberar(performance_id, nuevas, hold_id)
            raise

        return await self.consultar(performance_id, hold_id)

    async def consultar(self, performance_id: int, hold_id: str) -> EstadoDelApartado:
        """Lo que el comprador tiene apartado ahora mismo.

        Manda Redis: una butaca cuenta solo si su llave sigue siendo de
        este `hold_id`. El espejo de Postgres puede ir unos segundos atras.
        """
        ahora = await self._ahora()
        filas = (
            await self.db.execute(
                select(
                    PerformanceSeat.id,
                    PerformanceSeat.seat_id,
                    PerformanceSeat.precio_final,
                )
                .where(
                    PerformanceSeat.performance_id == performance_id,
                    PerformanceSeat.hold_id == hold_id,
                    PerformanceSeat.estado == APARTADO,
                )
                .order_by(PerformanceSeat.seat_id)
            )
        ).all()

        vigentes: list[ButacaApartada] = []
        ttls: list[int] = []
        for ps_id, seat_id, precio in filas:
            if not await self.holds.es_titular(performance_id, [seat_id], hold_id):
                continue
            ttl = await self.holds.ttl_restante(performance_id, seat_id)
            if ttl > 0:
                vigentes.append(ButacaApartada(seat_id, ps_id, precio))
                ttls.append(ttl)

        if not vigentes:
            raise RecursoNoEncontrado("El apartado no existe o ya vencio.")

        segundos = min(ttls)
        return EstadoDelApartado(
            hold_id=hold_id,
            performance_id=performance_id,
            butacas=tuple(vigentes),
            expira_en=ahora + timedelta(seconds=segundos),
            segundos_restantes=segundos,
        )

    async def liberar(
        self,
        performance_id: int,
        hold_id: str,
        seat_ids: Sequence[int] | None = None,
    ) -> int:
        """Devuelve butacas al inventario. Sin `seat_ids`, todas las del hold.

        Liberar lo que no es tuyo no hace nada, sin error: el `UPDATE`
        condicional y el Lua comparan dueño antes de tocar. Eso tambien
        lo hace idempotente, que es lo que se espera de un DELETE.
        """
        consulta = select(PerformanceSeat.seat_id).where(
            PerformanceSeat.performance_id == performance_id,
            PerformanceSeat.hold_id == hold_id,
            PerformanceSeat.estado == APARTADO,
        )
        if seat_ids is not None:
            consulta = consulta.where(PerformanceSeat.seat_id.in_(list(seat_ids)))
        suyas = list((await self.db.scalars(consulta)).all())
        if not suyas:
            return 0

        await self.holds.liberar(performance_id, suyas, hold_id)

        liberadas = list(
            (
                await self.db.scalars(
                    update(PerformanceSeat)
                    .where(
                        PerformanceSeat.performance_id == performance_id,
                        PerformanceSeat.seat_id.in_(suyas),
                        PerformanceSeat.hold_id == hold_id,
                        PerformanceSeat.estado == APARTADO,
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
        )
        if liberadas:
            await self.db.execute(
                insert(InventoryMove).values(
                    [
                        {
                            "performance_seat_id": ps_id,
                            "estado_previo": APARTADO,
                            "estado_nuevo": DISPONIBLE,
                            "motivo": "liberado por el comprador",
                            "hold_id": hold_id,
                        }
                        for ps_id in liberadas
                    ]
                )
            )
        await self.db.flush()
        return len(liberadas)

    # ----------------------------------------------------------- privados

    async def _ahora(self) -> datetime:
        """Hora de la base, no del servidor de aplicacion.

        Los vencimientos se escriben con `now()` de Postgres; compararlos
        contra el reloj de otra maquina mezclaria dos relojes.
        """
        return await self.db.scalar(select(func.now()))

    async def _funcion_a_la_venta(self, performance_id: int, ahora: datetime) -> None:
        fila = (
            await self.db.execute(
                select(Performance, Event, Client)
                .join(Event, Event.id == Performance.event_id)
                .join(Client, Client.id == Event.client_id)
                .where(Performance.id == performance_id)
            )
        ).first()

        # Borrador u organizador dado de baja: no existe para el publico.
        # Mismo error que una funcion inexistente.
        if fila is None or not visible_al_publico(fila[1], fila[2]):
            raise RecursoNoEncontrado(FUNCION_NO_EXISTE)

        motivo = motivo_no_se_vende(fila[0], ahora)
        if motivo is not None:
            mensaje, detalle = motivo
            raise FuncionNoALaVenta(
                mensaje, {"performance_id": performance_id, **detalle}
            )

    @staticmethod
    def _apartable(fila: PerformanceSeat, hold_id: str, ahora: datetime) -> bool:
        return _libre_para(
            fila.estado, fila.hold_id, fila.hold_expira_en, hold_id, ahora
        )

    async def _butaca_que_quedaria_suelta(
        self,
        performance_id: int,
        seat_ids: list[int],
        hold_id: str,
        ahora: datetime,
    ) -> int | None:
        """Aplica la regla de butacas sueltas a las filas que toca la compra.

        Lo que esta persona ya tenia apartado cuenta como parte de su
        seleccion: agregar una butaca a su carrito se evalua completo.
        """
        filas_tocadas = (
            await self.db.execute(
                select(Seat.zone_id, Seat.fila).where(Seat.id.in_(seat_ids)).distinct()
            )
        ).all()
        filas = (
            await self.db.execute(
                select(
                    PerformanceSeat.seat_id,
                    PerformanceSeat.estado,
                    PerformanceSeat.hold_id,
                    PerformanceSeat.hold_expira_en,
                    Seat.zone_id,
                    Seat.fila,
                    Seat.pos_x,
                )
                .join(Seat, Seat.id == PerformanceSeat.seat_id)
                .where(
                    PerformanceSeat.performance_id == performance_id,
                    tuple_(Seat.zone_id, Seat.fila).in_(filas_tocadas),
                )
            )
        ).all()

        lugares = [
            Lugar(
                seat_id=seat_id,
                zona_id=zona_id,
                fila=fila,
                x=x,
                libre=_libre_para(estado, dueno, expira, hold_id, ahora),
            )
            for seat_id, estado, dueno, expira, zona_id, fila, x in filas
        ]
        ya_suyas = {
            seat_id
            for seat_id, estado, dueno, *_ in filas
            if estado == APARTADO and dueno == hold_id
        }
        return butaca_suelta(lugares, set(seat_ids) | ya_suyas)

    async def _verificar_tope(
        self,
        performance_id: int,
        seat_ids: list[int],
        hold_id: str,
        ahora: datetime,
    ) -> None:
        """El tope es por apartado, no por peticion.

        Sin esto, un mismo `hold_id` acumularia ocho butacas por llamada
        sin limite, y el tope solo serviria contra quien no lo conoce.
        """
        otras = await self.db.scalar(
            select(func.count(PerformanceSeat.id)).where(
                PerformanceSeat.performance_id == performance_id,
                PerformanceSeat.hold_id == hold_id,
                PerformanceSeat.estado == APARTADO,
                PerformanceSeat.hold_expira_en > ahora,
                PerformanceSeat.seat_id.not_in(seat_ids),
            )
        )
        if otras + len(seat_ids) > settings.MAX_ASIENTOS_POR_ORDEN:
            raise LimiteDeAsientos(
                f"Maximo {settings.MAX_ASIENTOS_POR_ORDEN} butacas por compra.",
                {"ya_apartadas": otras, "solicitadas": len(seat_ids)},
            )

    async def _reflejar(
        self,
        performance_id: int,
        seat_ids: list[int],
        hold_id: str,
        ya_suyas: set[int],
        filas: dict[int, PerformanceSeat],
    ) -> None:
        """Escribe el apartado en `performance_seats`.

        El `UPDATE` repite la condicion de `_apartable` en la base: entre
        la lectura y esta escritura pudo llegar una venta o un bloqueo
        desde el panel. Si alguna fila no avanza, se pierde todo.
        """
        ttl_redis = min(
            [await self.holds.ttl_restante(performance_id, s) for s in seat_ids]
        )
        vigencia = timedelta(seconds=max(ttl_redis - MARGEN_ESPEJO_SEGUNDOS, 0))

        reflejadas = set(
            (
                await self.db.scalars(
                    update(PerformanceSeat)
                    .where(
                        PerformanceSeat.performance_id == performance_id,
                        PerformanceSeat.seat_id.in_(seat_ids),
                        or_(
                            PerformanceSeat.estado == DISPONIBLE,
                            and_(
                                PerformanceSeat.estado == APARTADO,
                                or_(
                                    PerformanceSeat.hold_id == hold_id,
                                    PerformanceSeat.hold_expira_en <= func.now(),
                                ),
                            ),
                        ),
                    )
                    .values(
                        estado=APARTADO,
                        hold_id=hold_id,
                        hold_expira_en=func.now() + vigencia,
                        version=PerformanceSeat.version + 1,
                    )
                    .returning(PerformanceSeat.seat_id)
                    .execution_options(synchronize_session=False)
                )
            ).all()
        )

        perdidas = [s for s in seat_ids if s not in reflejadas]
        if perdidas:
            raise AsientoNoDisponible(
                "Esa butaca ya no esta disponible.",
                {"seat_id": perdidas[0], "performance_id": performance_id},
            )

        movimientos = [
            {
                "performance_seat_id": filas[s].id,
                "estado_previo": filas[s].estado,
                "estado_nuevo": APARTADO,
                "motivo": "apartado por el comprador",
                "hold_id": hold_id,
            }
            for s in seat_ids
            if s not in ya_suyas
        ]
        if movimientos:
            await self.db.execute(insert(InventoryMove).values(movimientos))
        await self.db.flush()
