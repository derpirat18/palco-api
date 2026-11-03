"""Promociones (PR1): cuando aplican y cuanto descuentan.

Una sola calculadora para la compra en linea, la taquilla y lo que ve el
comprador: los tres cobran exactamente lo mismo.

Reglas (acordadas con PSD):

- **Cuando**: el dia de la semana *de la funcion*, a la hora del recinto (no
  el de la compra); que este encendida; dentro de "solo entre fechas" si la
  tiene; en ese evento y zona; en ese canal. Las cortesias nunca llevan
  promocion.
- **2x1**: por zona, los boletos que entran (solo adulto, o todos si
  `todos_los_tipos`) se ordenan del mas caro al mas barato y se hacen
  pares; en cada par el mas barato va sin costo. Sin compras impares: si
  sobra uno se pide un lugar mas (`ParIncompleto`), salvo que ya no quede
  ningun otro lugar libre en esa zona: entonces ese ultimo paga completo.
- **Porcentaje**: cada boleto que entra, redondeado al centavo.
- **No se suman**: si aplican dos, gana la de mayor descuento.
- **Comision**: sale de lo que paga el comprador (ya con descuento). Si PSD
  puso una comision especial, aplica a los boletos de la promocion.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import and_, delete, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import (
    EVENTO_NO_EXISTE,
    PROMOCION_NO_EXISTE,
    ErrorDeDominio,
    RecursoNoEncontrado,
)
from app.core.security import PermisoInsuficiente
from app.models.base import (
    EstadoAsiento,
    EstadoEvento,
    EstadoFuncion,
    RolUsuario,
)
from app.models.catalog import Client, Event, Performance, Seat, Venue, Zone
from app.models.inventory import PerformanceSeat
from app.models.promociones import (
    DOS_POR_UNO,
    PORCENTAJE,
    PORCENTAJE_MAXIMO,
    Promocion,
    PromocionEvento,
)
from app.models.sales import Order
from app.models.users import User
from app.services.tipos import ADULTO
from app.services.venta import al_centavo

LINEA = "linea"
TAQUILLA = "taquilla"
CERO = Decimal("0.00")
DIAS = ("lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo")
INICIAL = ("L", "M", "M", "J", "V", "S", "D")


class ParIncompleto(ErrorDeDominio):
    codigo = "par_incompleto"
    http_status = 409


class PromocionInvalida(ErrorDeDominio):
    codigo = "promocion_invalida"
    http_status = 422


class PromocionUsada(ErrorDeDominio):
    codigo = "promocion_usada"
    http_status = 409


# --------------------------------------------------------------------------
# La calculadora
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Regla:
    """Lo que la calculadora necesita de una promocion para una funcion."""

    id: int
    nombre: str
    tipo: str
    porcentaje: Decimal | None
    todos_los_tipos: bool
    # Zonas de la funcion donde aplica; None: todas.
    zonas: frozenset[int] | None
    comision_pct: Decimal | None = None

    def entra(self, zone_id: int, tipo: str) -> bool:
        if self.zonas is not None and zone_id not in self.zonas:
            return False
        return self.todos_los_tipos or tipo == ADULTO

    def publica(self) -> dict:
        """Lo que ve el comprador (y el equipo de la taquilla para vender sin red).
        Nunca la comision."""
        return {
            "id": self.id,
            "nombre": self.nombre,
            "tipo": self.tipo,
            "porcentaje": self.porcentaje,
            "todos_los_tipos": self.todos_los_tipos,
            "zonas": sorted(self.zonas) if self.zonas is not None else None,
        }


@dataclass(frozen=True)
class Lugar:
    clave: int
    zone_id: int
    tipo: str
    precio: Decimal


@dataclass(frozen=True)
class Resultado:
    regla: Regla | None = None
    descuentos: dict[int, Decimal] = field(default_factory=dict)
    # Boletos de la promocion (en un 2x1, los que pagan y los que no).
    alcanzados: frozenset[int] = frozenset()
    # Zonas donde sobra un boleto sin pareja y aun hay lugares libres.
    faltan: tuple[int, ...] = ()

    @property
    def descuento(self) -> Decimal:
        return sum(self.descuentos.values(), CERO)

    def descuento_de(self, clave: int) -> Decimal:
        return self.descuentos.get(clave, CERO)

    def comision_de(self, clave: int) -> Decimal | None:
        """La comision especial de ese boleto, o None para la de siempre."""
        if self.regla is None or self.regla.comision_pct is None:
            return None
        return self.regla.comision_pct if clave in self.alcanzados else None


SIN_PROMOCION = Resultado()


def aplicar(regla: Regla, lugares: Iterable[Lugar], ultimas: set[int]) -> Resultado:
    """Lo que descuenta una promocion. `ultimas`: zonas sin mas lugares libres
    fuera de esta seleccion (ahi el impar paga completo sin pedir otro)."""
    entran = [x for x in lugares if regla.entra(x.zone_id, x.tipo)]
    descuentos: dict[int, Decimal] = {}
    faltan: list[int] = []
    if regla.tipo == PORCENTAJE:
        pct = regla.porcentaje or CERO
        for x in entran:
            d = al_centavo(x.precio * pct / 100)
            if d > 0:
                descuentos[x.clave] = d
    else:
        por_zona: dict[int, list[Lugar]] = defaultdict(list)
        for x in entran:
            por_zona[x.zone_id].append(x)
        for zone_id, grupo in sorted(por_zona.items()):
            grupo.sort(key=lambda x: (-x.precio, x.clave))
            for i in range(1, len(grupo), 2):
                gratis = grupo[i]
                if gratis.precio > 0:
                    descuentos[gratis.clave] = gratis.precio
            if len(grupo) % 2 and zone_id not in ultimas:
                faltan.append(zone_id)
    return Resultado(
        regla, descuentos, frozenset(x.clave for x in entran), tuple(faltan)
    )


def mejor(
    reglas: Iterable[Regla], lugares: list[Lugar], ultimas: set[int]
) -> Resultado:
    """La que mas descuenta. Empate: la que pide completar un par (asi un 2x1
    no se salta) y luego la mas antigua."""
    candidatos = [aplicar(r, lugares, ultimas) for r in reglas]
    candidatos = [c for c in candidatos if c.descuento > 0 or c.faltan]
    if not candidatos:
        return SIN_PROMOCION
    return max(candidatos, key=lambda c: (c.descuento, bool(c.faltan), -c.regla.id))


def mensaje_de_par(resultado: Resultado, nombres: dict[int, str]) -> str:
    zonas = [nombres.get(z, "esa zona") for z in resultado.faltan]
    donde = " y en ".join(zonas)
    return (
        f"Con el 2x1 se compra en pares: elige 1 lugar más en {donde}, sin costo, "
        "o quita uno."
    )


# --------------------------------------------------------------------------
# Cuando aplica
# --------------------------------------------------------------------------


def fecha_local(momento: datetime, zona_horaria: str) -> date:
    return momento.astimezone(ZoneInfo(zona_horaria)).date()


def cae_en(dias: int, desde: date | None, hasta: date | None, dia: date) -> bool:
    if not dias & (1 << dia.weekday()):
        return False
    if desde is not None and dia < desde:
        return False
    return not (hasta is not None and dia > hasta)


async def _alcance(
    db: AsyncSession, promo_ids: list[int], event_id: int
) -> dict[int, frozenset[int] | None]:
    """Por promocion (que no es de todos los eventos): sus zonas en ese evento
    (None: todas). La que no aparece no aplica a ese evento."""
    if not promo_ids:
        return {}
    r: dict[int, set[int] | None] = {}
    for pid, zone_id in (
        await db.execute(
            select(PromocionEvento.promocion_id, PromocionEvento.zone_id).where(
                PromocionEvento.promocion_id.in_(promo_ids),
                PromocionEvento.event_id == event_id,
            )
        )
    ).all():
        if zone_id is None:
            r[pid] = None
        elif pid not in r or r[pid] is not None:
            r.setdefault(pid, set()).add(zone_id)
    return {k: frozenset(v) if v is not None else None for k, v in r.items()}


def _regla(p: Promocion, zonas: frozenset[int] | None) -> Regla:
    return Regla(
        id=p.id,
        nombre=p.nombre,
        tipo=p.tipo,
        porcentaje=p.porcentaje,
        todos_los_tipos=p.todos_los_tipos,
        zonas=zonas,
        comision_pct=p.comision_pct,
    )


async def reglas_por_funcion(
    db: AsyncSession,
    evento: Event,
    funciones: list[Performance],
    zona_horaria: str,
    canal: str,
    *,
    solo_encendidas: bool = True,
) -> dict[int, list[Regla]]:
    """Las promociones que aplican a cada funcion de un evento en ese canal."""
    if not funciones:
        return {}
    q = select(Promocion).where(Promocion.client_id == evento.client_id)
    q = q.where(Promocion.en_linea if canal == LINEA else Promocion.en_taquilla)
    if solo_encendidas:
        q = q.where(Promocion.activa.is_(True))
    promos = list((await db.scalars(q.order_by(Promocion.id))).all())
    if not promos:
        return {}
    alcance = await _alcance(
        db, [p.id for p in promos if not p.todos_los_eventos], evento.id
    )
    r: dict[int, list[Regla]] = {}
    for f in funciones:
        dia = fecha_local(f.inicia_en, zona_horaria)
        for p in promos:
            if not cae_en(p.dias, p.desde, p.hasta, dia):
                continue
            if p.todos_los_eventos:
                r.setdefault(f.id, []).append(_regla(p, None))
            elif p.id in alcance:
                r.setdefault(f.id, []).append(_regla(p, alcance[p.id]))
    return r


async def reglas_de(
    db: AsyncSession, funcion: Performance, evento: Event, canal: str
) -> list[Regla]:
    zh = await db.scalar(select(Venue.zona_horaria).where(Venue.id == evento.venue_id))
    return (await reglas_por_funcion(db, evento, [funcion], zh, canal)).get(
        funcion.id, []
    )


async def regla_guardada(
    db: AsyncSession, promocion_id: int, evento: Event
) -> Regla | None:
    """La promocion que el equipo de la taquilla aplico sin internet. Se
    respeta lo que se cobro aunque despues la hayan apagado o cambiado de
    dias: el dinero ya se recibio. Solo se revisa que sea de ese evento."""
    p = await db.get(Promocion, promocion_id)
    if p is None or p.client_id != evento.client_id:
        return None
    if p.todos_los_eventos:
        return _regla(p, None)
    alcance = await _alcance(db, [p.id], evento.id)
    if p.id not in alcance:
        return None
    return _regla(p, alcance[p.id])


async def zonas_sin_lugares(
    db: AsyncSession, performance_id: int, zone_ids: Iterable[int], excluir: list[int]
) -> set[int]:
    """Zonas donde no queda ningun lugar libre fuera de esta seleccion: ahi
    el ultimo boleto impar del 2x1 se vende completo."""
    zone_ids = list(set(zone_ids))
    if not zone_ids:
        return set()
    libre = or_(
        PerformanceSeat.estado == EstadoAsiento.DISPONIBLE.value,
        and_(
            PerformanceSeat.estado == EstadoAsiento.APARTADO.value,
            PerformanceSeat.hold_expira_en <= func.now(),
        ),
    )
    con_lugares = set(
        (
            await db.scalars(
                select(Seat.zone_id)
                .join(PerformanceSeat, PerformanceSeat.seat_id == Seat.id)
                .where(
                    PerformanceSeat.performance_id == performance_id,
                    Seat.zone_id.in_(zone_ids),
                    PerformanceSeat.id.not_in(excluir or [0]),
                    libre,
                )
                .distinct()
            )
        ).all()
    )
    return set(zone_ids) - con_lugares


async def cotizar(
    db: AsyncSession,
    reglas: list[Regla],
    performance_id: int,
    lugares: list[Lugar],
    excluir: list[int],
) -> Resultado:
    """La promocion que gana para esta seleccion. Solo consulta los lugares
    libres si hace falta (un 2x1 con un impar)."""
    if not reglas or not lugares:
        return SIN_PROMOCION
    resultado = mejor(reglas, lugares, set())
    if resultado.faltan:
        ultimas = await zonas_sin_lugares(db, performance_id, resultado.faltan, excluir)
        resultado = mejor(reglas, lugares, ultimas)
    return resultado


# --------------------------------------------------------------------------
# Panel: dar de alta, editar, encender y apagar
# --------------------------------------------------------------------------


@dataclass
class Datos:
    """Lo que se captura en los 4 pasos."""

    nombre: str
    tipo: str
    porcentaje: Decimal | None
    dias: list[int]
    todos_los_tipos: bool
    en_linea: bool
    en_taquilla: bool
    desde: date | None
    hasta: date | None
    todos_los_eventos: bool
    # [(event_id, [zone_id, ...] o None para todas)]
    eventos: list[tuple[int, list[int] | None]]
    activa: bool = True


def _bits(dias: list[int]) -> int:
    return sum(1 << d for d in set(dias))


def dias_de(bits: int) -> list[int]:
    return [d for d in range(7) if bits & (1 << d)]


def dias_en_palabras(bits: int) -> str:
    d = dias_de(bits)
    if len(d) == 7:
        return "Todos los días"
    texto = _lista([DIAS[x] for x in d])
    return texto[0].upper() + texto[1:]


def _validar(d: Datos) -> None:
    if d.tipo not in (DOS_POR_UNO, PORCENTAJE):
        raise PromocionInvalida("Escoge 2x1 o porcentaje.", {"campo": "tipo"})
    if d.tipo == PORCENTAJE and (
        d.porcentaje is None or not (1 <= d.porcentaje <= PORCENTAJE_MAXIMO)
    ):
        raise PromocionInvalida(
            "El porcentaje va de 1 a 90 %.", {"campo": "porcentaje"}
        )
    if not d.dias or any(x not in range(7) for x in d.dias):
        raise PromocionInvalida("Escoge al menos un día.", {"campo": "dias"})
    if not (d.en_linea or d.en_taquilla):
        raise PromocionInvalida(
            "Escoge dónde se vende: en línea, en taquilla o en las dos.",
            {"campo": "en_linea"},
        )
    if d.desde and d.hasta and d.desde > d.hasta:
        raise PromocionInvalida(
            "La fecha «hasta» va después de la fecha «desde».", {"campo": "hasta"}
        )
    if not d.todos_los_eventos and not d.eventos:
        raise PromocionInvalida(
            "Escoge al menos un evento, o «Todos mis eventos».", {"campo": "eventos"}
        )


async def zonas_de_eventos(
    db: AsyncSession, event_ids: list[int]
) -> dict[int, list[Zone]]:
    """Las zonas de los mapas que usan las funciones de cada evento."""
    if not event_ids:
        return {}
    filas = (
        await db.execute(
            select(Performance.event_id, Zone)
            .join(Zone, Zone.seat_map_id == Performance.seat_map_id)
            .where(Performance.event_id.in_(event_ids))
            .distinct()
            .order_by(Performance.event_id, Zone.orden, Zone.id)
        )
    ).all()
    r: dict[int, list[Zone]] = defaultdict(list)
    for eid, z in filas:
        if all(x.id != z.id for x in r[eid]):
            r[eid].append(z)
    return r


async def _revisar_eventos(db: AsyncSession, client_id: int, d: Datos) -> None:
    """Los eventos y zonas tienen que ser de ese organizador. Lo ajeno se
    responde como inexistente."""
    if d.todos_los_eventos:
        return
    ids = [e for e, _ in d.eventos]
    if len(set(ids)) != len(ids):
        raise PromocionInvalida("Hay eventos repetidos.", {"campo": "eventos"})
    propios = set(
        (
            await db.scalars(
                select(Event.id).where(Event.id.in_(ids), Event.client_id == client_id)
            )
        ).all()
    )
    if propios != set(ids):
        raise RecursoNoEncontrado(EVENTO_NO_EXISTE)
    zonas = await zonas_de_eventos(db, ids)
    for eid, zs in d.eventos:
        if zs is None:
            continue
        if not zs:
            raise PromocionInvalida(
                "Escoge al menos una zona, o «Todas las zonas».", {"campo": "eventos"}
            )
        validas = {z.id for z in zonas.get(eid, [])}
        if not set(zs) <= validas:
            raise PromocionInvalida(
                "Hay zonas que no son de ese evento.", {"campo": "eventos"}
            )


def _es_psd(usuario: User) -> bool:
    return usuario.rol == RolUsuario.ADMIN_PSD


async def guardar(
    db: AsyncSession,
    usuario: User,
    client_id: int,
    datos: Datos,
    promo: Promocion | None = None,
    comision: tuple[bool, Decimal | None] = (False, None),
) -> Promocion:
    """Crea (`promo` None) o reemplaza una promocion. `comision`: (si se
    mando, valor). Solo PSD la cambia."""
    _validar(datos)
    mando, valor = comision
    if mando and not _es_psd(usuario):
        actual = promo.comision_pct if promo else None
        if valor != actual:
            raise PermisoInsuficiente(
                "Solo PSD cambia la comisión de una promoción.",
                {"campo": "comision_pct"},
            )
    if await db.get(Client, client_id) is None:
        raise RecursoNoEncontrado(PROMOCION_NO_EXISTE)
    await _revisar_eventos(db, client_id, datos)
    if promo is None:
        promo = Promocion(client_id=client_id, creada_por=usuario.id)
        db.add(promo)
    promo.nombre = datos.nombre
    promo.tipo = datos.tipo
    promo.porcentaje = datos.porcentaje if datos.tipo == PORCENTAJE else None
    promo.dias = _bits(datos.dias)
    promo.todos_los_tipos = datos.todos_los_tipos
    promo.en_linea = datos.en_linea
    promo.en_taquilla = datos.en_taquilla
    promo.desde = datos.desde
    promo.hasta = datos.hasta
    promo.todos_los_eventos = datos.todos_los_eventos
    promo.activa = datos.activa
    promo.actualizada_por = usuario.id
    if mando and _es_psd(usuario):
        promo.comision_pct = valor
    await db.flush()
    await db.execute(
        delete(PromocionEvento).where(PromocionEvento.promocion_id == promo.id)
    )
    if not datos.todos_los_eventos:
        for eid, zs in datos.eventos:
            for zid in zs if zs is not None else [None]:
                db.add(
                    PromocionEvento(promocion_id=promo.id, event_id=eid, zone_id=zid)
                )
    await db.flush()
    await db.refresh(promo)
    return promo


async def cargar(db: AsyncSession, promocion_id: int) -> Promocion:
    promo = await db.get(Promocion, promocion_id)
    if promo is None:
        raise RecursoNoEncontrado(PROMOCION_NO_EXISTE)
    return promo


async def usada(db: AsyncSession, promocion_id: int) -> bool:
    return bool(
        await db.scalar(select(exists().where(Order.promocion_id == promocion_id)))
    )


async def borrar(db: AsyncSession, promo: Promocion) -> None:
    if await usada(db, promo.id):
        raise PromocionUsada(
            "Esta promoción ya se usó en compras, así que no se borra: apágala y "
            "deja de aplicarse.",
        )
    await db.delete(promo)
    await db.flush()


# --------------------------------------------------------------------------
# Panel: lo que se muestra
# --------------------------------------------------------------------------


async def _funciones_proximas(
    db: AsyncSession, client_id: int, event_ids: list[int] | None
) -> list[tuple[Performance, Event, Venue]]:
    q = (
        select(Performance, Event, Venue)
        .join(Event, Event.id == Performance.event_id)
        .join(Venue, Venue.id == Event.venue_id)
        .where(
            Event.client_id == client_id,
            Event.estado != EstadoEvento.ARCHIVADO.value,
            Performance.estado.not_in(
                (EstadoFuncion.CANCELADA.value, EstadoFuncion.REALIZADA.value)
            ),
            Performance.inicia_en > func.now(),
        )
        .order_by(Performance.inicia_en)
    )
    if event_ids is not None:
        q = q.where(Event.id.in_(event_ids or [0]))
    return list((await db.execute(q)).all())


def _alcance_de_datos(d: Datos) -> dict[int, frozenset[int] | None] | None:
    if d.todos_los_eventos:
        return None
    return {e: frozenset(z) if z is not None else None for e, z in d.eventos}


async def vista_previa(
    db: AsyncSession, client_id: int, d: Datos, propia_id: int | None = None
) -> dict:
    """Las funciones que caerian en la promocion y las otras promociones que
    tambien caen en alguna de ellas (para avisar cual gana)."""
    alcance = _alcance_de_datos(d)
    bits = _bits(d.dias)
    filas = await _funciones_proximas(
        db, client_id, None if alcance is None else list(alcance)
    )
    zonas = await zonas_de_eventos(db, list({e.id for _, e, _ in filas}))
    nombres = {z.id: z.nombre for zs in zonas.values() for z in zs}
    funciones = []
    por_evento: dict[int, list[Performance]] = defaultdict(list)
    for f, e, v in filas:
        dia = fecha_local(f.inicia_en, v.zona_horaria)
        if not bits or not cae_en(bits, d.desde, d.hasta, dia):
            continue
        zs = None if alcance is None else alcance.get(e.id)
        funciones.append(
            {
                "performance_id": f.id,
                "evento": e.nombre,
                "ciudad": v.ciudad,
                "inicia_en": f.inicia_en.astimezone(ZoneInfo(v.zona_horaria))
                .replace(tzinfo=None)
                .isoformat(),
                "zonas": "Todas las zonas"
                if zs is None
                else _lista(sorted({nombres.get(z, "") for z in zs} - {""})),
            }
        )
        por_evento[e.id].append(f)
    conflictos: dict[int, dict] = {}
    eventos = {e.id: (e, v) for _, e, v in filas}
    for eid, fs in por_evento.items():
        e, v = eventos[eid]
        for canal in ((LINEA,) if d.en_linea else ()) + (
            (TAQUILLA,) if d.en_taquilla else ()
        ):
            otras = await reglas_por_funcion(
                db, e, fs, v.zona_horaria, canal, solo_encendidas=False
            )
            for rs in otras.values():
                for r in rs:
                    if r.id == propia_id or r.id in conflictos:
                        continue
                    p = await db.get(Promocion, r.id)
                    conflictos[r.id] = {
                        "id": p.id,
                        "nombre": p.nombre,
                        "activa": p.activa,
                        "valor": valor_en_texto(p),
                    }
    return {
        "funciones": funciones[:40],
        "total_funciones": len(funciones),
        "conflictos": list(conflictos.values()),
    }


def _lista(nombres: list[str]) -> str:
    if not nombres:
        return ""
    if len(nombres) == 1:
        return nombres[0]
    return f"{', '.join(nombres[:-1])} y {nombres[-1]}"


def valor_en_texto(p: Promocion) -> str:
    if p.tipo == DOS_POR_UNO:
        return "2x1"
    return f"{p.porcentaje.normalize():f} %"


async def vista(db: AsyncSession, promo: Promocion) -> dict:
    """Una promocion como la pinta el panel."""
    filas = (
        await db.execute(
            select(PromocionEvento.event_id, PromocionEvento.zone_id).where(
                PromocionEvento.promocion_id == promo.id
            )
        )
    ).all()
    eventos: dict[int, list[int] | None] = {}
    for eid, zid in filas:
        if zid is None:
            eventos[eid] = None
        elif eid not in eventos or eventos[eid] is not None:
            eventos.setdefault(eid, []).append(zid)
    nombres_ev = dict(
        (
            await db.execute(
                select(Event.id, Event.nombre).where(Event.id.in_(list(eventos) or [0]))
            )
        ).all()
    )
    zonas = await zonas_de_eventos(db, list(eventos))
    nombre_zona = {z.id: z.nombre for zs in zonas.values() for z in zs}
    datos = Datos(
        nombre=promo.nombre,
        tipo=promo.tipo,
        porcentaje=promo.porcentaje,
        dias=dias_de(promo.dias),
        todos_los_tipos=promo.todos_los_tipos,
        en_linea=promo.en_linea,
        en_taquilla=promo.en_taquilla,
        desde=promo.desde,
        hasta=promo.hasta,
        todos_los_eventos=promo.todos_los_eventos,
        eventos=list(eventos.items()),
    )
    previa = await vista_previa(db, promo.client_id, datos, promo.id)
    organizador = await db.scalar(
        select(Client.nombre).where(Client.id == promo.client_id)
    )
    autor = (
        await db.scalar(select(User.nombre).where(User.id == promo.actualizada_por))
        if promo.actualizada_por
        else None
    )
    return {
        "id": promo.id,
        "client_id": promo.client_id,
        "organizador": organizador,
        "nombre": promo.nombre,
        "tipo": promo.tipo,
        "porcentaje": promo.porcentaje,
        "valor": valor_en_texto(promo),
        "dias": dias_de(promo.dias),
        "dias_texto": dias_en_palabras(promo.dias),
        "todos_los_tipos": promo.todos_los_tipos,
        "en_linea": promo.en_linea,
        "en_taquilla": promo.en_taquilla,
        "desde": promo.desde,
        "hasta": promo.hasta,
        "todos_los_eventos": promo.todos_los_eventos,
        "eventos": [
            {
                "event_id": eid,
                "nombre": nombres_ev.get(eid, ""),
                "zonas": zs,
                "zonas_texto": "Todas las zonas"
                if zs is None
                else _lista([nombre_zona.get(z, "") for z in zs]),
            }
            for eid, zs in eventos.items()
        ],
        "comision_pct": promo.comision_pct,
        "activa": promo.activa,
        "usada": await usada(db, promo.id),
        "proxima": previa["funciones"][0] if previa["funciones"] else None,
        "funciones_proximas": previa["total_funciones"],
        "actualizada_por": autor,
        "actualizado_en": promo.actualizado_en,
    }


async def listar(db: AsyncSession, client_id: int | None) -> list[dict]:
    q = select(Promocion).order_by(Promocion.activa.desc(), Promocion.nombre)
    if client_id is not None:
        q = q.where(Promocion.client_id == client_id)
    return [await vista(db, p) for p in (await db.scalars(q)).all()]


async def opciones(db: AsyncSession, client_id: int) -> dict:
    """Los eventos del organizador (no archivados) con sus zonas, agrupadas
    por nombre: un evento con dos mapas tiene dos "Luneta"."""
    eventos = list(
        (
            await db.execute(
                select(Event, Venue)
                .join(Venue, Venue.id == Event.venue_id)
                .where(
                    Event.client_id == client_id,
                    Event.estado != EstadoEvento.ARCHIVADO.value,
                )
                .order_by(Event.nombre)
            )
        ).all()
    )
    ids = [e.id for e, _ in eventos]
    zonas = await zonas_de_eventos(db, ids)
    proximas = dict(
        (
            await db.execute(
                select(Performance.event_id, func.count(Performance.id))
                .where(
                    Performance.event_id.in_(ids or [0]),
                    Performance.inicia_en > func.now(),
                    Performance.estado.not_in(
                        (EstadoFuncion.CANCELADA.value, EstadoFuncion.REALIZADA.value)
                    ),
                )
                .group_by(Performance.event_id)
            )
        ).all()
    )
    r = []
    for e, v in eventos:
        grupos: dict[str, list[int]] = {}
        for z in zonas.get(e.id, []):
            grupos.setdefault(z.nombre, []).append(z.id)
        r.append(
            {
                "event_id": e.id,
                "nombre": e.nombre,
                "recinto": v.nombre,
                "ciudad": v.ciudad,
                "funciones": int(proximas.get(e.id, 0)),
                "zonas": [{"nombre": n, "ids": i} for n, i in grupos.items()],
            }
        )
    return {"eventos": r}
