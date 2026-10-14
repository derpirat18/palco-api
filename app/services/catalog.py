"""Catalogo: recintos, mapas, eventos y funciones.

Los recintos y sus mapas son infraestructura compartida: los administra
PSD y cualquier organizador puede programar funciones en ellos. Los
eventos pertenecen a un organizador y nadie mas los ve.
"""

from __future__ import annotations

import re
import unicodedata
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import (
    EVENTO_NO_EXISTE,
    FUNCION_NO_EXISTE,
    ErrorDeDominio,
    RecursoNoEncontrado,
)
from app.models.catalog import (
    Event,
    Performance,
    PerformancePrice,
    Seat,
    SeatMap,
    Venue,
    Zone,
)
from app.services.seat_maps import MapaGenerado


class ConflictoDeCatalogo(ErrorDeDominio):
    codigo = "conflicto_de_catalogo"
    http_status = 409


def slugify(texto: str) -> str:
    """Slug a partir del nombre del evento, para la URL publica."""
    sin_acentos = (
        unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode()
    )
    s = re.sub(r"[^a-zA-Z0-9]+", "-", sin_acentos).strip("-").lower()
    return re.sub(r"-{2,}", "-", s)[:160] or "evento"


class CatalogService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    # ---------------------------------------------------------------- mapas

    async def guardar_mapa(self, venue_id: int, mapa: MapaGenerado) -> SeatMap:
        """Persiste un mapa generado o importado.

        Las butacas se insertan en lote: un teatro mediano son miles de
        filas y hacerlo una por una tarda minutos.
        """
        recinto = await self.db.get(Venue, venue_id)
        if recinto is None:
            raise RecursoNoEncontrado("El recinto no existe.")

        repetido = await self.db.scalar(
            select(SeatMap).where(
                SeatMap.venue_id == venue_id, SeatMap.nombre == mapa.nombre
            )
        )
        if repetido is not None:
            raise ConflictoDeCatalogo(
                f"El recinto ya tiene un mapa llamado '{mapa.nombre}'."
            )

        seat_map = SeatMap(
            venue_id=venue_id, nombre=mapa.nombre, viewport=mapa.viewport
        )
        self.db.add(seat_map)
        await self.db.flush()

        for zona_gen in mapa.zonas:
            zona = Zone(
                seat_map_id=seat_map.id,
                nombre=zona_gen.nombre,
                color=zona_gen.color,
                orden=zona_gen.orden,
            )
            self.db.add(zona)
            await self.db.flush()

            self.db.add_all(
                [
                    Seat(
                        zone_id=zona.id,
                        fila=b.fila,
                        numero=b.numero,
                        pos_x=b.pos_x,
                        pos_y=b.pos_y,
                        kill=b.kill,
                    )
                    for b in zona_gen.butacas
                ]
            )

        await self.db.flush()
        return seat_map

    async def contar_butacas(self, seat_map_id: int) -> tuple[int, int]:
        """Devuelve (totales, vendibles) de un mapa."""
        total = await self.db.scalar(
            select(func.count(Seat.id))
            .join(Zone, Zone.id == Seat.zone_id)
            .where(Zone.seat_map_id == seat_map_id)
        )
        vendibles = await self.db.scalar(
            select(func.count(Seat.id))
            .join(Zone, Zone.id == Seat.zone_id)
            .where(Zone.seat_map_id == seat_map_id, Seat.kill.is_(False))
        )
        return int(total or 0), int(vendibles or 0)

    async def mapa_en_uso(self, seat_map_id: int) -> bool:
        """Un mapa con funciones programadas no se puede modificar."""
        return bool(
            await self.db.scalar(
                select(func.count(Performance.id)).where(
                    Performance.seat_map_id == seat_map_id
                )
            )
        )

    # --------------------------------------------------------------- eventos

    async def slug_disponible(self, base: str, event_id: int | None = None) -> str:
        """Agrega sufijo numerico si el slug ya existe.

        Dos plazas del mismo espectaculo generan el mismo slug, y la URL
        publica tiene que ser unica.
        """
        slug = base
        n = 1
        while True:
            consulta = select(Event).where(Event.slug == slug)
            if event_id is not None:
                consulta = consulta.where(Event.id != event_id)
            if await self.db.scalar(consulta) is None:
                return slug
            n += 1
            slug = f"{base}-{n}"

    async def obtener_evento(self, event_id: int) -> Event:
        evento = await self.db.get(Event, event_id)
        if evento is None:
            raise RecursoNoEncontrado(EVENTO_NO_EXISTE)
        return evento

    async def obtener_funcion(self, performance_id: int) -> Performance:
        funcion = await self.db.get(Performance, performance_id)
        if funcion is None:
            raise RecursoNoEncontrado(FUNCION_NO_EXISTE)
        return funcion

    # -------------------------------------------------------------- precios

    async def fijar_precios(
        self, performance_id: int, precios_por_zona: dict[int, Decimal]
    ) -> list[PerformancePrice]:
        """Define el precio base de cada zona para una funcion.

        Reemplaza los precios existentes. El precio efectivo de cada
        butaca se recalcula al materializar el inventario; las que tengan
        precio personalizado no se tocan.
        """
        funcion = await self.obtener_funcion(performance_id)

        zonas_validas = set(
            (
                await self.db.scalars(
                    select(Zone.id).where(Zone.seat_map_id == funcion.seat_map_id)
                )
            ).all()
        )
        ajenas = set(precios_por_zona) - zonas_validas
        if ajenas:
            raise ConflictoDeCatalogo(
                "Hay zonas que no pertenecen al mapa de esta funcion.",
                {"zonas": sorted(ajenas)},
            )

        existentes = list(
            (
                await self.db.scalars(
                    select(PerformancePrice).where(
                        PerformancePrice.performance_id == performance_id
                    )
                )
            ).all()
        )
        por_zona = {p.zone_id: p for p in existentes}

        resultado = []
        for zone_id, precio in precios_por_zona.items():
            fila = por_zona.get(zone_id)
            if fila is None:
                fila = PerformancePrice(
                    performance_id=performance_id, zone_id=zone_id, precio=precio
                )
                self.db.add(fila)
            else:
                fila.precio = precio
                fila.activo = True
            resultado.append(fila)

        # Las zonas que ya no traen precio quedan inactivas, no se borran:
        # si hay ordenes que las referencian, el historico se conserva.
        for zone_id, fila in por_zona.items():
            if zone_id not in precios_por_zona:
                fila.activo = False

        await self.db.flush()
        return resultado
