"""Mapa de una funcion para el panel (C4): de donde se dan las cortesias.

A diferencia del publico, este si dice en que esta cada butaca (vendida,
cortesia, apartada, bloqueada): lo ve solo PSD o el organizador dueno.
`libre` es exactamente lo que `CortesiaService` deja tomar (disponible,
bloqueada o con apartado vencido), para que lo que aqui se elige se pueda
dar. Solo lee: no aparta ni cambia nada.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.base import EstadoAsiento
from app.models.catalog import Performance, Seat, SeatMap, Zone
from app.models.inventory import PerformanceSeat


@dataclass(frozen=True)
class ZonaDelPanel:
    id: int
    nombre: str
    color: str | None
    precio_desde: Decimal | None


@dataclass(frozen=True)
class ButacaDelPanel:
    id: int
    zona_id: int
    fila: str
    numero: str
    x: Decimal
    y: Decimal
    # libre: se puede dar como cortesia. ocupada: no.
    estado: str
    # Lo que de verdad tiene: disponible, bloqueado, apartado, vendido, cortesia.
    situacion: str
    precio: Decimal


@dataclass(frozen=True)
class MapaDelPanel:
    viewport: dict
    zonas: tuple[ZonaDelPanel, ...]
    butacas: tuple[ButacaDelPanel, ...]


class MapaPanelService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def de_funcion(self, funcion: Performance) -> MapaDelPanel:
        mapa = await self.db.get(SeatMap, funcion.seat_map_id)
        zonas = (
            await self.db.scalars(
                select(Zone).where(Zone.seat_map_id == mapa.id).order_by(Zone.orden)
            )
        ).all()
        tomable = or_(
            PerformanceSeat.estado.in_(
                (EstadoAsiento.DISPONIBLE.value, EstadoAsiento.BLOQUEADO.value)
            ),
            and_(
                PerformanceSeat.estado == EstadoAsiento.APARTADO.value,
                PerformanceSeat.hold_expira_en <= func.now(),
            ),
        )
        filas = (
            await self.db.execute(
                select(
                    Seat.id,
                    Seat.zone_id,
                    Seat.fila,
                    Seat.numero,
                    Seat.pos_x,
                    Seat.pos_y,
                    PerformanceSeat.estado,
                    PerformanceSeat.precio_final,
                    tomable,
                )
                .join(PerformanceSeat, PerformanceSeat.seat_id == Seat.id)
                .where(
                    PerformanceSeat.performance_id == funcion.id,
                    PerformanceSeat.estado != EstadoAsiento.KILL.value,
                )
                .order_by(Seat.zone_id, Seat.fila, Seat.id)
            )
        ).all()
        butacas = tuple(
            ButacaDelPanel(
                id=sid,
                zona_id=zid,
                fila=f,
                numero=n,
                x=x,
                y=y,
                estado="libre" if libre else "ocupada",
                situacion=situacion,
                precio=precio,
            )
            for sid, zid, f, n, x, y, situacion, precio, libre in filas
        )
        desde: dict[int, Decimal] = {}
        for b in butacas:
            if b.estado == "libre" and (
                b.zona_id not in desde or b.precio < desde[b.zona_id]
            ):
                desde[b.zona_id] = b.precio
        return MapaDelPanel(
            viewport=mapa.viewport or {},
            zonas=tuple(
                ZonaDelPanel(z.id, z.nombre, z.color, desde.get(z.id)) for z in zonas
            ),
            butacas=butacas,
        )
