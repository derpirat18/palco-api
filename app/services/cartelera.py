"""La cartelera: los eventos que se pueden comprar, con busqueda y filtros.

Es publica, asi que siguen las reglas de la pagina del evento: no salen el
cliente, la comision ni conteos de butacas, y la disponibilidad va en tres
niveles. Las visitas solo sirven para ordenar "lo mas buscado"; nunca se
muestran.

Los filtros se aplican aqui y no en SQL: el catalogo de una boletera son
decenas o cientos de eventos, y asi la busqueda ignora acentos y
mayusculas ("cristobal" encuentra "San Cristobal") sin extensiones de
Postgres. Si el catalogo crece a miles, se pasa a SQL con `unaccent`.
"""

from __future__ import annotations

import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.base import EstadoEvento
from app.models.catalog import Client, Event, Performance, Venue
from app.services.publico import (
    NO_SE_MUESTRAN,
    FuncionPublica,
    PublicoService,
    RecintoPublico,
)
from app.services.venta import iva_de

# Del mas al menos atractivo para resumir varias funciones en una tarjeta.
_ORDEN_DE_VENTA = ("en_venta", "proximamente", "agotada", "venta_cerrada")
_ORDEN_DE_DISPONIBILIDAD = ("disponible", "ultimos_lugares", "agotado")


def clave_de_visitas(evento_uuid: UUID) -> str:
    # Por uuid y no por slug: el slug cambia si se renombra el evento.
    return f"visitas:evento:{evento_uuid}"


def normalizar(texto: str | None) -> str:
    """Sin acentos, sin mayusculas y sin espacios de sobra."""
    if not texto:
        return ""
    sin_acentos = "".join(
        c for c in unicodedata.normalize("NFKD", texto) if not unicodedata.combining(c)
    )
    return " ".join(sin_acentos.casefold().split())


@dataclass(frozen=True)
class TarjetaDeEvento:
    uuid: UUID
    slug: str
    nombre: str
    categoria: str | None
    imagen_url: str | None
    destacado: bool
    recinto: RecintoPublico
    proxima_funcion: datetime
    # Cuantas funciones proximas tiene (no butacas).
    funciones: int
    # El mejor estado entre sus funciones: si una esta en venta, la
    # tarjeta dice "en venta".
    estado_venta: str
    disponibilidad: str | None
    precio_desde: Decimal | None
    # Meses (AAAA-MM, en la hora del recinto) en que tiene funciones.
    meses: tuple[str, ...]
    visitas: int


@dataclass(frozen=True)
class CiudadConEventos:
    nombre: str
    pais: str
    eventos: int


@dataclass(frozen=True)
class FiltrosDeCartelera:
    paises: tuple[str, ...]
    # De la mas buscada a la menos.
    ciudades: tuple[CiudadConEventos, ...]
    categorias: tuple[str, ...]
    meses: tuple[str, ...]


@dataclass(frozen=True)
class Cartelera:
    eventos: tuple[TarjetaDeEvento, ...]
    total: int
    filtros: FiltrosDeCartelera


@dataclass(frozen=True)
class Busqueda:
    texto: str | None = None
    pais: str | None = None
    ciudad: str | None = None
    mes: str | None = None
    categoria: str | None = None
    solo_destacados: bool = False
    # fecha | populares
    orden: str = "fecha"
    limite: int = 50


class CarteleraService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.publico = PublicoService(db)

    async def cartelera(
        self, busqueda: Busqueda, visitas: dict[UUID, int] | None = None
    ) -> Cartelera:
        """Las opciones de los filtros salen de toda la cartelera, no de lo
        filtrado: asi el comprador puede cambiar de ciudad sin quitar filtros.
        """
        tarjetas = await self.tarjetas(visitas or {})
        filtros = _filtros(tarjetas)
        elegidas = [t for t in tarjetas if _coincide(t, busqueda)]
        if busqueda.orden == "populares":
            elegidas.sort(key=lambda t: (-t.visitas, t.proxima_funcion))
        else:
            elegidas.sort(key=lambda t: (t.proxima_funcion, t.nombre))
        return Cartelera(
            eventos=tuple(elegidas[: busqueda.limite]),
            total=len(elegidas),
            filtros=filtros,
        )

    async def visibles(self) -> list[UUID]:
        """Para leer de Redis las visitas de lo que si se muestra."""
        return [t.uuid for t in await self.tarjetas({})]

    async def tarjetas(self, visitas: dict[UUID, int]) -> list[TarjetaDeEvento]:
        ahora: datetime = await self.db.scalar(select(func.now()))
        filas = (
            await self.db.execute(
                select(Event, Venue)
                .join(Client, Client.id == Event.client_id)
                .join(Venue, Venue.id == Event.venue_id)
                # La misma regla que `visible_al_publico`, en SQL.
                .where(
                    Event.estado == EstadoEvento.PUBLICADO.value,
                    Client.activo.is_(True),
                )
            )
        ).all()
        if not filas:
            return []

        por_evento: dict[int, list[Performance]] = defaultdict(list)
        for funcion in (
            await self.db.scalars(
                select(Performance)
                .where(
                    Performance.event_id.in_([e.id for e, _ in filas]),
                    Performance.estado.not_in(NO_SE_MUESTRAN),
                    Performance.inicia_en > ahora,
                )
                .order_by(Performance.inicia_en)
            )
        ).all():
            por_evento[funcion.event_id].append(funcion)

        ids = [f.id for fs in por_evento.values() for f in fs]
        inventario = await self.publico._inventario(ids)
        precio_de_zona = await self.publico._precio_minimo_de_zona(ids)

        tarjetas = []
        for evento, recinto in filas:
            funciones = por_evento.get(evento.id)
            if not funciones:
                continue
            iva = iva_de(evento)
            publicas = [
                self.publico._funcion_publica(
                    f,
                    ahora,
                    inventario.get(f.id),
                    iva.precio_al_publico(precio_de_zona[f.id])
                    if f.id in precio_de_zona
                    else None,
                )
                for f in funciones
            ]
            zona = ZoneInfo(recinto.zona_horaria)
            tarjetas.append(
                TarjetaDeEvento(
                    uuid=evento.uuid,
                    slug=evento.slug,
                    nombre=evento.nombre,
                    categoria=evento.categoria,
                    imagen_url=evento.imagen_url,
                    destacado=evento.destacado,
                    recinto=RecintoPublico(
                        nombre=recinto.nombre,
                        ciudad=recinto.ciudad,
                        estado_geo=recinto.estado_geo,
                        pais=recinto.pais,
                        zona_horaria=recinto.zona_horaria,
                    ),
                    proxima_funcion=funciones[0].inicia_en,
                    funciones=len(funciones),
                    estado_venta=_mejor(
                        [p.estado_venta for p in publicas], _ORDEN_DE_VENTA
                    ),
                    disponibilidad=_disponibilidad(publicas),
                    precio_desde=min(
                        (
                            p.precio_desde
                            for p in publicas
                            if p.precio_desde is not None
                        ),
                        default=None,
                    ),
                    meses=tuple(
                        sorted(
                            {
                                f.inicia_en.astimezone(zona).strftime("%Y-%m")
                                for f in funciones
                            }
                        )
                    ),
                    visitas=visitas.get(evento.uuid, 0),
                )
            )
        return tarjetas


def _mejor(valores: list[str], orden: tuple[str, ...]) -> str:
    return min(valores, key=orden.index)


def _disponibilidad(publicas: list[FuncionPublica]) -> str | None:
    """La de las funciones que se pueden comprar; si ninguna, la de todas."""
    en_venta = [p.disponibilidad for p in publicas if p.estado_venta == "en_venta"]
    conocidas = [d for d in (en_venta or [p.disponibilidad for p in publicas]) if d]
    return _mejor(conocidas, _ORDEN_DE_DISPONIBILIDAD) if conocidas else None


def _coincide(t: TarjetaDeEvento, b: Busqueda) -> bool:
    if b.solo_destacados and not t.destacado:
        return False
    if b.pais and t.recinto.pais.upper() != b.pais.strip().upper():
        return False
    if b.ciudad and normalizar(t.recinto.ciudad) != normalizar(b.ciudad):
        return False
    if b.categoria and normalizar(t.categoria) != normalizar(b.categoria):
        return False
    if b.mes and b.mes not in t.meses:
        return False
    if b.texto:
        donde = normalizar(
            " ".join([t.nombre, t.recinto.nombre, t.recinto.ciudad, t.categoria or ""])
        )
        # Todas las palabras, en cualquier orden: "circo cristobal".
        if not all(palabra in donde for palabra in normalizar(b.texto).split()):
            return False
    return True


def _filtros(tarjetas: list[TarjetaDeEvento]) -> FiltrosDeCartelera:
    ciudades: dict[tuple[str, str], list[TarjetaDeEvento]] = defaultdict(list)
    for t in tarjetas:
        ciudades[(t.recinto.ciudad, t.recinto.pais)].append(t)
    return FiltrosDeCartelera(
        paises=tuple(sorted({t.recinto.pais for t in tarjetas})),
        ciudades=tuple(
            CiudadConEventos(nombre=c, pais=p, eventos=len(ts))
            for (c, p), ts in sorted(
                ciudades.items(),
                key=lambda kv: (-sum(t.visitas for t in kv[1]), -len(kv[1]), kv[0][0]),
            )
        ),
        categorias=tuple(sorted({t.categoria for t in tarjetas if t.categoria})),
        meses=tuple(sorted({m for t in tarjetas for m in t.meses})),
    )
