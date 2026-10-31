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
from app.models.catalog import Event, Performance, Seat, SeatMap, Zone
from app.models.inventory import PerformanceSeat
from app.services.tipos import TIPOS, nombre_con_edad, tarifas_de
from app.services.venta import iva_de


@dataclass(frozen=True)
class ZonaDelPanel:
    id: int
    nombre: str
    color: str | None
    precio_desde: Decimal | None
    tarifas: tuple[dict, ...] = ()
    # Entrada general (EG): se vende por cantidad, no se dibuja.
    general: bool = False


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
    solo_adulto: bool = False


@dataclass(frozen=True)
class MapaDelPanel:
    viewport: dict
    zonas: tuple[ZonaDelPanel, ...]
    butacas: tuple[ButacaDelPanel, ...]


class MapaPanelService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def de_funcion(
        self,
        funcion: Performance,
        con_bloqueadas: bool = True,
        reserva: str | None = None,
    ) -> MapaDelPanel:
        """`con_bloqueadas`: las cortesias si toman bloqueadas; la taquilla (TQ) no.
        `reserva`: el apartado de una caja; sus lugares los puede vender ella."""
        mapa = await self.db.get(SeatMap, funcion.seat_map_id)
        zonas = (
            await self.db.scalars(
                select(Zone).where(Zone.seat_map_id == mapa.id).order_by(Zone.orden)
            )
        ).all()
        origenes = (EstadoAsiento.DISPONIBLE.value,) + (
            (EstadoAsiento.BLOQUEADO.value,) if con_bloqueadas else ()
        )
        tomable = or_(
            PerformanceSeat.estado.in_(origenes),
            and_(
                PerformanceSeat.estado == EstadoAsiento.APARTADO.value,
                or_(
                    PerformanceSeat.hold_expira_en <= func.now(),
                    PerformanceSeat.hold_id == (reserva or ""),
                ),
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
                    PerformanceSeat.precio_personalizado,
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
                solo_adulto=propio,
            )
            for sid, zid, f, n, x, y, situacion, precio, libre, propio in filas
        )
        desde: dict[int, Decimal] = {}
        for b in butacas:
            if b.estado == "libre" and (
                b.zona_id not in desde or b.precio < desde[b.zona_id]
            ):
                desde[b.zona_id] = b.precio
        evento = await self.db.get(Event, funcion.event_id)
        tarifas = await tarifas_de(self.db, funcion.id, iva_de(evento))

        def de_zona(zone_id: int) -> tuple[dict, ...]:
            return tuple(
                {"tipo": t, "nombre": nombre_con_edad(t, evento), "precio": p}
                for t, p in sorted(
                    tarifas.de_zona(zone_id).items(), key=lambda x: TIPOS.index(x[0])
                )
            )

        return MapaDelPanel(
            viewport=mapa.viewport or {},
            zonas=tuple(
                ZonaDelPanel(
                    z.id,
                    z.nombre,
                    z.color,
                    desde.get(z.id),
                    de_zona(z.id),
                    general=z.general,
                )
                for z in zonas
            ),
            butacas=butacas,
        )
