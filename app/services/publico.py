"""Lo que ve el comprador sin sesion: la pagina de un evento por su link.

Todo aqui es publico, asi que importa tanto lo que se muestra como lo que
no. No salen el cliente, la comision de PSD, los conteos de butacas ni
identificadores internos del organizador. La disponibilidad se da en tres
niveles y no en numeros: cuantas butacas quedan es informacion comercial
del organizador, no del publico.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import EVENTO_NO_EXISTE, RecursoNoEncontrado
from app.models.base import EstadoAsiento, EstadoFuncion
from app.models.catalog import Client, Event, Performance, PerformancePrice, Venue
from app.models.inventory import PerformanceSeat
from app.services.venta import Cargos, cargos_de, motivo_no_se_vende, visible_al_publico

# Por debajo de esta fraccion de butacas libres se anuncia "ultimos lugares".
# Es la regla del prototipo: menos del 12 % del inventario vendible.
UMBRAL_ULTIMOS_LUGARES_PCT = 12

# Estados que nunca se ofrecen. Una butaca en kill o bloqueada no cuenta
# ni como libre ni como parte del aforo vendible.
FUERA_DE_VENTA = (EstadoAsiento.KILL.value, EstadoAsiento.BLOQUEADO.value)

# Funciones que no aparecen en la pagina.
NO_SE_MUESTRAN = (EstadoFuncion.CANCELADA.value, EstadoFuncion.REALIZADA.value)


@dataclass(frozen=True)
class RecintoPublico:
    nombre: str
    ciudad: str
    estado_geo: str | None
    pais: str
    zona_horaria: str


@dataclass(frozen=True)
class FuncionPublica:
    id: int
    inicia_en: datetime
    apertura_puertas: datetime | None
    # en_venta | proximamente | agotada | venta_cerrada
    estado_venta: str
    venta_abre_en: datetime | None
    # disponible | ultimos_lugares | agotado; None si aun no hay inventario
    disponibilidad: str | None
    precio_desde: Decimal | None
    precio_desde_con_cargo: Decimal | None


@dataclass(frozen=True)
class MedicionPublica:
    """IDs para que el front cargue la medicion del organizador.

    Son publicos por naturaleza: cualquier pagina que los usa los deja en
    su codigo fuente. Sin ellos el organizador no ve sus visitas ni sus
    compras en su propio GA4 o en Meta.
    """

    ga4_id: str | None
    meta_pixel_id: str | None


@dataclass(frozen=True)
class EventoPublico:
    slug: str
    nombre: str
    categoria: str | None
    descripcion: str | None
    imagen_url: str | None
    recinto: RecintoPublico
    cargos: Cargos
    medicion: MedicionPublica
    funciones: tuple[FuncionPublica, ...]


@dataclass(frozen=True)
class _Inventario:
    libres: int
    vendibles: int
    desde: Decimal | None


class PublicoService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def evento_por_slug(self, slug: str) -> EventoPublico:
        fila = (
            await self.db.execute(
                select(Event, Client, Venue)
                .join(Client, Client.id == Event.client_id)
                .join(Venue, Venue.id == Event.venue_id)
                .where(Event.slug == slug.strip().lower())
            )
        ).first()
        if fila is None or not visible_al_publico(fila[0], fila[1]):
            raise RecursoNoEncontrado(EVENTO_NO_EXISTE)
        evento, cliente, recinto = fila

        ahora: datetime = await self.db.scalar(select(func.now()))
        funciones = list(
            (
                await self.db.scalars(
                    select(Performance)
                    .where(
                        Performance.event_id == evento.id,
                        Performance.estado.not_in(NO_SE_MUESTRAN),
                        Performance.inicia_en > ahora,
                    )
                    .order_by(Performance.inicia_en)
                )
            ).all()
        )

        cargos = cargos_de(evento, cliente)
        ids = [f.id for f in funciones]
        inventario = await self._inventario(ids)
        precio_de_zona = await self._precio_minimo_de_zona(ids)

        return EventoPublico(
            slug=evento.slug,
            nombre=evento.nombre,
            categoria=evento.categoria,
            descripcion=evento.descripcion,
            imagen_url=evento.imagen_url,
            recinto=RecintoPublico(
                nombre=recinto.nombre,
                ciudad=recinto.ciudad,
                estado_geo=recinto.estado_geo,
                pais=recinto.pais,
                zona_horaria=recinto.zona_horaria,
            ),
            cargos=cargos,
            medicion=MedicionPublica(
                ga4_id=cliente.ga_id, meta_pixel_id=cliente.pixel_id
            ),
            funciones=tuple(
                self._funcion_publica(
                    f, ahora, cargos, inventario.get(f.id), precio_de_zona.get(f.id)
                )
                for f in funciones
            ),
        )

    # ----------------------------------------------------------- privados

    @staticmethod
    def _funcion_publica(
        funcion: Performance,
        ahora: datetime,
        cargos: Cargos,
        inventario: _Inventario | None,
        precio_de_zona: Decimal | None,
    ) -> FuncionPublica:
        """Traduce las reglas de venta al estado que ve el comprador.

        Usa la misma regla que el apartado (`motivo_no_se_vende`): si esta
        pagina dice "en venta", apartar funciona.
        """
        motivo = motivo_no_se_vende(funcion, ahora)
        clave = motivo[1]["motivo"] if motivo else None

        if clave == "venta_cerrada":
            estado_venta = "venta_cerrada"
        elif funcion.estado == EstadoFuncion.AGOTADA.value or (
            clave is None and inventario is not None and inventario.libres == 0
        ):
            estado_venta = "agotada"
        elif clave is None:
            estado_venta = "en_venta"
        else:
            # Programada, sin inventario o con fecha de apertura futura.
            estado_venta = "proximamente"

        if inventario is None:
            disponibilidad = None
            desde = precio_de_zona
        else:
            disponibilidad = _nivel(inventario)
            desde = inventario.desde

        if estado_venta in ("agotada", "venta_cerrada"):
            desde = None

        return FuncionPublica(
            id=funcion.id,
            inicia_en=funcion.inicia_en,
            apertura_puertas=funcion.apertura_puertas,
            estado_venta=estado_venta,
            venta_abre_en=funcion.venta_abre_en,
            disponibilidad=disponibilidad,
            precio_desde=desde,
            precio_desde_con_cargo=(
                cargos.con_cargo_de_servicio(desde) if desde is not None else None
            ),
        )

    async def _inventario(self, performance_ids: list[int]) -> dict[int, _Inventario]:
        """Libres, vendibles y precio mas bajo libre, por funcion.

        Una butaca libre es la disponible o la apartada cuyo apartado ya
        vencio en Postgres: es la misma condicion con la que el apartado
        la deja pasar. Solo funciones con inventario aparecen aqui.
        """
        if not performance_ids:
            return {}
        libre = or_(
            PerformanceSeat.estado == EstadoAsiento.DISPONIBLE.value,
            and_(
                PerformanceSeat.estado == EstadoAsiento.APARTADO.value,
                PerformanceSeat.hold_expira_en <= func.now(),
            ),
        )
        filas = (
            await self.db.execute(
                select(
                    PerformanceSeat.performance_id,
                    func.count().filter(libre),
                    func.count().filter(PerformanceSeat.estado.not_in(FUERA_DE_VENTA)),
                    func.min(PerformanceSeat.precio_final).filter(libre),
                )
                .where(PerformanceSeat.performance_id.in_(performance_ids))
                .group_by(PerformanceSeat.performance_id)
            )
        ).all()
        return {
            pid: _Inventario(libres=int(lib), vendibles=int(ven), desde=desde)
            for pid, lib, ven, desde in filas
        }

    async def _precio_minimo_de_zona(
        self, performance_ids: list[int]
    ) -> dict[int, Decimal]:
        """Para funciones sin inventario todavia: el precio de zona mas bajo."""
        if not performance_ids:
            return {}
        filas = (
            await self.db.execute(
                select(
                    PerformancePrice.performance_id, func.min(PerformancePrice.precio)
                )
                .where(
                    PerformancePrice.performance_id.in_(performance_ids),
                    PerformancePrice.activo.is_(True),
                )
                .group_by(PerformancePrice.performance_id)
            )
        ).all()
        return dict(filas)


def _nivel(inventario: _Inventario) -> str:
    if inventario.libres == 0:
        return "agotado"
    if inventario.libres * 100 < inventario.vendibles * UMBRAL_ULTIMOS_LUGARES_PCT:
        return "ultimos_lugares"
    return "disponible"
