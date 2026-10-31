"""Catalogo: recintos, mapas, eventos y funciones.

Los recintos y sus mapas son infraestructura compartida: los administra
PSD y cualquier organizador puede programar funciones en ellos. Los
eventos pertenecen a un organizador y nadie mas los ve.
"""

from __future__ import annotations

import re
import unicodedata
from decimal import Decimal

from sqlalchemy import delete, exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import (
    EVENTO_NO_EXISTE,
    FUNCION_NO_EXISTE,
    ErrorDeDominio,
    RecursoNoEncontrado,
)
from app.models.base import EstadoEvento
from app.models.catalog import (
    DireccionAnterior,
    Event,
    Performance,
    PerformancePrice,
    Seat,
    SeatMap,
    Venue,
    Zone,
)
from app.services.seat_maps import ButacaGenerada, MapaGenerado, ZonaGenerada


class ConflictoDeCatalogo(ErrorDeDominio):
    codigo = "conflicto_de_catalogo"
    http_status = 409


class DireccionNoValida(ErrorDeDominio):
    """La direccion personalizada no se puede usar (URL1)."""

    codigo = "direccion_no_valida"
    http_status = 409


# Las primeras partes de la direccion que ya usa (o usara) el sitio: un
# evento llamado "panel" se comeria la entrada al panel.
DIRECCIONES_RESERVADAS = frozenset(
    {
        "admin", "api", "assets", "ayuda", "boletos", "cartelera", "contacto",
        "crear-cuenta", "cuenta", "entrar", "evento", "eventos", "img",
        "legal", "mis-boletos", "orden", "ordenes", "panel", "perfil",
        "pruebas", "salir", "soporte", "static", "www",
    }
)  # fmt: skip
DIRECCION_MIN = 3
DIRECCION_MAX = 80


def _slug(texto: str) -> str:
    sin_acentos = (
        unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode()
    )
    s = re.sub(r"[^a-zA-Z0-9]+", "-", sin_acentos).strip("-").lower()
    return re.sub(r"-{2,}", "-", s)[:160]


def slugify(texto: str) -> str:
    """Slug a partir del nombre del evento, para la URL publica."""
    return _slug(texto) or "evento"


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
                general=zona_gen.general,
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

    async def leer_mapa(self, seat_map_id: int) -> MapaGenerado:
        """Un mapa guardado, con la misma forma que uno recien generado: asi
        el panel lo dibuja igual que la vista previa."""
        seat_map = await self.db.get(SeatMap, seat_map_id)
        if seat_map is None:
            raise RecursoNoEncontrado("El mapa de butacas no existe.")
        zonas = list(
            (
                await self.db.scalars(
                    select(Zone)
                    .where(Zone.seat_map_id == seat_map_id)
                    .order_by(Zone.orden, Zone.id)
                )
            ).all()
        )
        por_zona: dict[int, ZonaGenerada] = {
            z.id: ZonaGenerada(nombre=z.nombre, color=z.color, orden=z.orden)
            for z in zonas
        }
        for seat in (
            await self.db.scalars(
                select(Seat)
                .where(Seat.zone_id.in_(list(por_zona)))
                .order_by(Seat.zone_id, Seat.id)
            )
        ).all():
            zona = por_zona[seat.zone_id]
            zona.butacas.append(
                ButacaGenerada(
                    zona=zona.nombre,
                    fila=seat.fila,
                    numero=seat.numero,
                    pos_x=seat.pos_x,
                    pos_y=seat.pos_y,
                    kill=seat.kill,
                )
            )
        return MapaGenerado(
            nombre=seat_map.nombre,
            viewport=seat_map.viewport,
            zonas=list(por_zona.values()),
        )

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

    async def slug_ocupado(self, slug: str, event_id: int | None = None) -> bool:
        """Si la direccion es de otro evento, hoy o antes.

        Una direccion vieja sigue llevando a su evento (URL1): si otro la
        tomara, las ligas que ya circulan llevarian a un evento ajeno.
        """
        actual = select(Event.id).where(Event.slug == slug)
        anterior = select(DireccionAnterior.id).where(DireccionAnterior.slug == slug)
        if event_id is not None:
            actual = actual.where(Event.id != event_id)
            anterior = anterior.where(DireccionAnterior.event_id != event_id)
        return bool(await self.db.scalar(select(exists(actual) | exists(anterior))))

    async def slug_disponible(self, base: str, event_id: int | None = None) -> str:
        """Agrega sufijo numerico si el slug ya existe.

        Dos plazas del mismo espectaculo generan el mismo slug, y la URL
        publica tiene que ser unica. Tampoco toma una reservada del sitio.
        """
        slug = base
        n = 1
        while slug in DIRECCIONES_RESERVADAS or await self.slug_ocupado(slug, event_id):
            n += 1
            slug = f"{base}-{n}"
        return slug

    @staticmethod
    def es_slug_automatico(slug: str, nombre: str) -> bool:
        """Si la direccion es la que Palco saco del nombre (con o sin -2, -3).

        Asi un cambio de nombre en borrador la actualiza, pero no pisa una
        que el organizador eligio a mano.
        """
        base = slugify(nombre)
        return slug == base or bool(re.fullmatch(re.escape(base) + r"-\d+", slug))

    async def cambiar_direccion(
        self, evento: Event, texto: str, usuario_id: int | None
    ) -> None:
        """Pone la direccion personalizada del evento (URL1).

        Se normaliza como las automaticas ("Atayde Coatzacoalcos 2026" ->
        atayde-coatzacoalcos-2026). Si el evento ya salio al publico, la
        direccion anterior se guarda para que sus ligas sigan funcionando;
        un borrador no tiene ligas circulando y no aparta direcciones.
        """
        nuevo = _slug(texto)
        if not DIRECCION_MIN <= len(nuevo) <= DIRECCION_MAX:
            raise DireccionNoValida(
                f"La dirección lleva de {DIRECCION_MIN} a {DIRECCION_MAX} "
                "letras, números o guiones.",
                {"campo": "slug"},
            )
        if nuevo == evento.slug:
            return
        if nuevo in DIRECCIONES_RESERVADAS:
            raise DireccionNoValida(
                "Esa dirección la usa el sitio. Elige otra.", {"campo": "slug"}
            )
        if await self.slug_ocupado(nuevo, evento.id):
            raise DireccionNoValida(
                "Esa dirección ya la usa otro evento. Elige otra.", {"campo": "slug"}
            )
        # Volver a una direccion que el evento ya tuvo: deja de ser "anterior".
        await self.db.execute(
            delete(DireccionAnterior).where(
                DireccionAnterior.event_id == evento.id,
                DireccionAnterior.slug == nuevo,
            )
        )
        if evento.estado != EstadoEvento.BORRADOR.value:
            self.db.add(
                DireccionAnterior(
                    event_id=evento.id, slug=evento.slug, cambiado_por=usuario_id
                )
            )
        evento.slug = nuevo

    async def obtener_evento(self, event_id: int) -> Event:
        evento = await self.db.get(Event, event_id)
        if evento is None:
            raise RecursoNoEncontrado(EVENTO_NO_EXISTE)
        return evento

    async def verificar_cambio_de_iva(self, evento: Event, cambios: dict) -> None:
        """Cambiar como se capturan los precios no puede mover lo que ya se vende.

        Con "IVA incluido", cambiar la tasa solo cambia el desglose de las
        compras siguientes: se permite siempre. Pero pasar a "mas IVA" (o
        cambiar la tasa estando en "mas IVA") cambia lo que paga el
        comprador, y las butacas ya generadas tienen su precio. Eso solo se
        decide antes de generar inventario.

        Un `null` explicito en estos campos se ignora: no hay evento sin
        tratamiento de IVA.
        """
        for campo in ("iva_pct", "precios_incluyen_iva"):
            if campo in cambios and cambios[campo] is None:
                del cambios[campo]

        incluido = cambios.get("precios_incluyen_iva", evento.precios_incluyen_iva)
        pct = cambios.get("iva_pct", evento.iva_pct)
        cambia_precio = incluido != evento.precios_incluyen_iva or (
            not incluido and pct != evento.iva_pct
        )
        if not cambia_precio:
            return

        con_inventario = await self.db.scalar(
            select(
                exists().where(
                    Performance.event_id == evento.id,
                    Performance.inventario_generado.is_(True),
                )
            )
        )
        if con_inventario:
            raise ConflictoDeCatalogo(
                "Este evento ya tiene inventario: cambiar cómo se capturan los "
                "precios cambiaría lo que paga el comprador. Se decide antes "
                "de generar el inventario.",
                {"campos": sorted({"iva_pct", "precios_incluyen_iva"} & set(cambios))},
            )

    async def obtener_funcion(self, performance_id: int) -> Performance:
        funcion = await self.db.get(Performance, performance_id)
        if funcion is None:
            raise RecursoNoEncontrado(FUNCION_NO_EXISTE)
        return funcion

    # -------------------------------------------------------------- precios

    async def precios_vigentes(self, performance_id: int) -> list[PerformancePrice]:
        """Los precios base activos de la funcion, para mostrarlos al editar."""
        return list(
            (
                await self.db.scalars(
                    select(PerformancePrice)
                    .where(
                        PerformancePrice.performance_id == performance_id,
                        PerformancePrice.activo.is_(True),
                    )
                    .order_by(PerformancePrice.zone_id)
                )
            ).all()
        )

    async def fijar_precios(
        self,
        performance_id: int,
        precios_por_zona: dict[int, Decimal],
        tipos: dict[int, tuple[Decimal | None, Decimal | None]] | None = None,
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
        ajenas = (set(precios_por_zona) | set(tipos or {})) - zonas_validas
        if ajenas:
            raise ConflictoDeCatalogo(
                "Hay zonas que no pertenecen al mapa de esta función.",
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
            if tipos is not None:
                fila.precio_nino, fila.precio_mayor = tipos.get(zone_id, (None, None))
            resultado.append(fila)

        # Las zonas que ya no traen precio quedan inactivas, no se borran:
        # si hay ordenes que las referencian, el historico se conserva.
        for zone_id, fila in por_zona.items():
            if zone_id not in precios_por_zona:
                fila.activo = False

        await self.db.flush()
        return resultado
