"""Reportes del panel (RP3): Ventas, Taquilla y cortes, Asistencia, Cortesias,
Compradores y Ocupacion.

Reglas:

- **Quien**: PSD ve todo; el organizador, lo de sus eventos; el jefe de
  taquilla, solo Taquilla y cortes de los eventos donde es jefe. Cajeros y
  puerta, nada.
- **Periodo**: por fecha de compra o por fecha de la funcion (lo escoge
  quien consulta). Los dias son de la hora del centro de Mexico.
- **Lo vendido no se recalcula**: se suma lo congelado en cada orden
  (importe, IVA, `comision_pct_snap`). La comision de PSD es sobre el total
  que pago el comprador (ADR 0012). La de Stripe aun no se guarda por orden
  (P9): el neto todavia no la descuenta y el reporte lo dice.
- **Datos personales**: Compradores solo trae correo y telefono de quien
  acepto recibir promociones en una compra de ese organizador, y siempre
  los de esa compra (`*_snap`), nunca los del registro. Ver o descargar
  Compradores, y descargar cualquier reporte, queda en la bitacora
  (`data_access_log`) con los filtros.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ErrorDeDominio
from app.core.security import PermisoInsuficiente
from app.models.acceso import ConsultaDeDatos
from app.models.base import EstadoAsiento, EstadoBoleto, EstadoOrden, RolUsuario
from app.models.catalog import Event, Performance, Seat, Venue, Zone
from app.models.inventory import PerformanceSeat
from app.models.sales import Consent, Cortesia, Customer, Order, OrderItem, Ticket
from app.models.taquilla import (
    CERRADA,
    CONTADA,
    JEFE,
    CajaDeTaquilla,
    PersonalDeTaquilla,
    VentaDeTaquilla,
)
from app.models.users import User

ZONA = ZoneInfo("America/Mexico_City")
CERO = Decimal("0")
CENTAVO = Decimal("0.01")
VISTA_PREVIA = 200
MAXIMO_DE_FILAS = 100_000
VENDIDAS = (
    EstadoOrden.PAGADA.value,
    EstadoOrden.CANCELADA.value,
    EstadoOrden.REEMBOLSADA.value,
)
CANALES = {"linea": "En línea", "taquilla": "Taquilla", "cortesia": "Cortesía"}
TIPOS = {"adulto": "Adulto", "nino": "Niño", "mayor": "Adulto mayor"}
FORMAS = {"efectivo": "Efectivo", "terminal": "Terminal", "cortesia": "Cortesía"}
ESTADOS_CAJA = {"abierta": "Abierta", "contada": "Contada", "cerrada": "Cerrada"}


class ReporteInvalido(ErrorDeDominio):
    codigo = "reporte_invalido"
    http_status = 422


# --------------------------------------------------------------------------
# Lo que devuelve un reporte
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Columna:
    clave: str
    titulo: str
    # texto, entero, dinero, pct, fecha, fechahora
    formato: str = "texto"


@dataclass(frozen=True)
class Indicador:
    etiqueta: str
    valor: Any
    formato: str = "entero"


@dataclass
class Grafica:
    titulo: str
    etiquetas: list[str]
    valores: list[float]
    formato: str = "entero"


@dataclass
class Reporte:
    tipo: str
    titulo: str
    indicadores: list[Indicador]
    columnas: list[Columna]
    filas: list[dict[str, Any]]
    grafica: Grafica | None = None
    notas: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Filtros:
    desde: date | None = None
    hasta: date | None = None
    por: str = "compra"  # compra | funcion
    client_id: int | None = None
    event_id: int | None = None
    performance_id: int | None = None
    zone_id: int | None = None
    canal: str | None = None
    forma_pago: str | None = None
    tipo: str | None = None
    cajero_id: int | None = None
    estado: str | None = None

    def rango(self) -> tuple[datetime | None, datetime | None]:
        """Del inicio de `desde` al final de `hasta`, en hora del centro."""
        ini = (
            datetime.combine(self.desde, time.min, ZONA).astimezone(UTC)
            if self.desde
            else None
        )
        fin = (
            datetime.combine(self.hasta + timedelta(days=1), time.min, ZONA).astimezone(
                UTC
            )
            if self.hasta
            else None
        )
        return ini, fin

    def como_texto(self) -> dict[str, Any]:
        return {
            k: (str(v) if v is not None else None) for k, v in self.__dict__.items()
        }


@dataclass(frozen=True)
class Definicion:
    tipo: str
    titulo: str
    pregunta: str
    filtros: tuple[str, ...]
    por_omision: str


CATALOGO: tuple[Definicion, ...] = (
    Definicion(
        "ventas",
        "Ventas",
        "¿Cuánto vendí, de qué y por dónde?",
        ("periodo", "evento", "funcion", "zona", "canal", "tipo", "estado"),
        "compra",
    ),
    Definicion(
        "taquilla",
        "Taquilla y cortes",
        "¿Cómo cerró cada caja?",
        ("periodo", "evento", "cajero"),
        "compra",
    ),
    Definicion(
        "asistencia",
        "Asistencia",
        "¿Cuánta gente entró?",
        ("periodo", "evento", "funcion"),
        "funcion",
    ),
    Definicion(
        "cortesias",
        "Cortesías",
        "¿Cuántas regalamos y por qué?",
        ("periodo", "evento", "funcion", "zona"),
        "compra",
    ),
    Definicion(
        "compradores",
        "Compradores",
        "¿Quiénes me compraron?",
        ("periodo", "evento", "funcion", "canal"),
        "compra",
    ),
    Definicion(
        "ocupacion",
        "Ocupación",
        "¿Cómo va cada función?",
        ("periodo", "evento", "funcion", "zona"),
        "funcion",
    ),
    Definicion(
        "promociones",
        "Promociones",
        "¿Cuánto vendió cada promoción?",
        ("periodo", "evento", "funcion", "canal"),
        "compra",
    ),
)
POR_TIPO = {d.tipo: d for d in CATALOGO}
PERSONALES = {"compradores"}


# --------------------------------------------------------------------------
# Permisos
# --------------------------------------------------------------------------


async def eventos_de_jefe(db: AsyncSession, usuario: User) -> list[int]:
    return list(
        (
            await db.scalars(
                select(PersonalDeTaquilla.event_id).where(
                    PersonalDeTaquilla.user_id == usuario.id,
                    PersonalDeTaquilla.puesto == JEFE,
                )
            )
        ).all()
    )


async def disponibles(db: AsyncSession, usuario: User) -> list[Definicion]:
    if usuario.rol in (RolUsuario.ADMIN_PSD, RolUsuario.ORGANIZADOR):
        return list(CATALOGO)
    if await eventos_de_jefe(db, usuario):
        return [POR_TIPO["taquilla"]]
    return []


async def acotar(db: AsyncSession, usuario: User, tipo: str, f: Filtros) -> Filtros:
    """Los filtros que de verdad aplican a quien consulta: el organizador nunca
    sale de lo suyo, y el jefe de taquilla solo ve sus eventos."""
    if tipo not in POR_TIPO:
        raise ReporteInvalido("Ese reporte no existe.")
    if f.por not in ("compra", "funcion"):
        raise ReporteInvalido("El periodo es por fecha de compra o de la función.")
    if f.desde and f.hasta and f.desde > f.hasta:
        raise ReporteInvalido(
            "La fecha inicial va antes de la final.", {"campo": "desde"}
        )
    if usuario.rol == RolUsuario.ADMIN_PSD:
        return f
    if usuario.rol == RolUsuario.ORGANIZADOR:
        return Filtros(**{**f.__dict__, "client_id": usuario.client_id})
    jefe = await eventos_de_jefe(db, usuario)
    if tipo != "taquilla" or not jefe:
        raise PermisoInsuficiente("Tu cuenta no tiene acceso a este reporte.")
    if f.event_id is not None and f.event_id not in jefe:
        raise PermisoInsuficiente("Tu cuenta no tiene acceso a este evento.")
    return Filtros(**{**f.__dict__, "client_id": usuario.client_id})


async def registrar(
    db: AsyncSession, usuario: User, tipo: str, accion: str, f: Filtros, formato: str
) -> None:
    db.add(
        ConsultaDeDatos(
            user_id=usuario.id,
            recurso=f"reporte_{tipo}",
            event_id=f.event_id,
            performance_id=f.performance_id,
            accion=accion,
            filtros={**f.como_texto(), "formato": formato},
        )
    )
    await db.flush()


# --------------------------------------------------------------------------
# Ayudantes
# --------------------------------------------------------------------------


def _local(momento: datetime | None, zona: str | None = None) -> datetime | None:
    if momento is None:
        return None
    return momento.astimezone(ZoneInfo(zona) if zona else ZONA).replace(tzinfo=None)


def _dinero(x) -> Decimal:
    return Decimal(x or 0).quantize(CENTAVO, rounding=ROUND_HALF_UP)


def _pct(parte, total) -> Decimal | None:
    if not total:
        return None
    return (Decimal(parte) * 100 / Decimal(total)).quantize(Decimal("0.1"))


def _canal(orden_canal: str | None) -> str:
    if orden_canal in ("taquilla", "cortesia"):
        return orden_canal
    return "linea"


def _alcance(q, f: Filtros):
    """Organizador, evento y funcion: siempre sobre Event y Performance."""
    if f.client_id is not None:
        q = q.where(Event.client_id == f.client_id)
    if f.event_id is not None:
        q = q.where(Event.id == f.event_id)
    if f.performance_id is not None:
        q = q.where(Performance.id == f.performance_id)
    return q


def _periodo(q, f: Filtros, fecha_compra):
    ini, fin = f.rango()
    columna = Performance.inicia_en if f.por == "funcion" else fecha_compra
    if ini is not None:
        q = q.where(columna >= ini)
    if fin is not None:
        q = q.where(columna < fin)
    return q


def _filtro_canal(q, canal: str | None):
    if canal == "taquilla":
        return q.where(Order.canal == "taquilla")
    if canal == "cortesia":
        return q.where(Order.canal == "cortesia")
    if canal == "linea":
        return q.where(
            or_(Order.canal.is_(None), Order.canal.not_in(("taquilla", "cortesia")))
        )
    return q


def _por_dia(montos: dict[date, Decimal]) -> Grafica | None:
    if not montos:
        return None
    dias = sorted(montos)
    if (dias[-1] - dias[0]).days > 62:
        meses: dict[str, Decimal] = defaultdict(lambda: CERO)
        for d in dias:
            meses[d.strftime("%m/%Y")] += montos[d]
        return Grafica(
            "Venta por mes",
            list(meses),
            [float(v) for v in meses.values()],
            "dinero",
        )
    return Grafica(
        "Venta por día",
        [d.strftime("%d/%m") for d in dias],
        [float(montos[d]) for d in dias],
        "dinero",
    )


# --------------------------------------------------------------------------
# Ventas
# --------------------------------------------------------------------------


async def _ventas(db: AsyncSession, f: Filtros) -> Reporte:
    q = (
        select(
            Event.nombre,
            Performance.inicia_en,
            Venue.zona_horaria,
            OrderItem.zona_snap,
            OrderItem.tipo,
            Order.canal,
            Order.estado,
            OrderItem.anulado,
            OrderItem.importe,
            OrderItem.iva,
            func.coalesce(OrderItem.comision_pct_snap, Order.comision_pct_snap),
            Order.pagada_en,
            Order.id,
            OrderItem.descuento,
        )
        .join(Order, Order.id == OrderItem.order_id)
        .join(Performance, Performance.id == Order.performance_id)
        .join(Event, Event.id == Performance.event_id)
        .join(Venue, Venue.id == Event.venue_id)
        .join(PerformanceSeat, PerformanceSeat.id == OrderItem.performance_seat_id)
        .join(Seat, Seat.id == PerformanceSeat.seat_id)
        .where(Order.estado.in_(VENDIDAS), Order.pagada_en.is_not(None))
    )
    q = _periodo(_alcance(q, f), f, Order.pagada_en)
    q = _filtro_canal(q, f.canal)
    if f.zone_id is not None:
        q = q.where(Seat.zone_id == f.zone_id)
    if f.tipo:
        q = q.where(OrderItem.tipo == f.tipo)
    if f.estado:
        q = q.where(Order.estado == f.estado)
    filas = (await db.execute(q.limit(MAXIMO_DE_FILAS))).all()

    grupos: dict[tuple, dict[str, Any]] = {}
    ordenes: set[int] = set()
    por_dia: dict[date, Decimal] = defaultdict(lambda: CERO)
    tot = defaultdict(lambda: CERO)
    for (
        evento,
        inicia,
        zh,
        zona,
        tipo,
        canal_orden,
        estado,
        anulado,
        importe,
        iva,
        pct,
        pagada,
        oid,
        descuento,
    ) in filas:
        canal = _canal(canal_orden)
        clave = (evento, inicia, zona, tipo, canal)
        g = grupos.setdefault(
            clave,
            {
                "evento": evento,
                "funcion": _local(inicia, zh),
                "zona": zona,
                "tipo": TIPOS.get(tipo, tipo),
                "canal": CANALES[canal],
                "boletos": 0,
                "venta_bruta": CERO,
                "descuento": CERO,
                "iva": CERO,
                "comision": CERO,
                "neto": CERO,
                "cancelados": 0,
                "reembolsado": CERO,
            },
        )
        vigente = estado == EstadoOrden.PAGADA.value and not anulado
        if vigente:
            comision = _dinero(importe * pct / 100)
            g["boletos"] += 1
            g["venta_bruta"] += importe
            g["descuento"] += descuento
            tot["descuento"] += descuento
            g["iva"] += iva
            g["comision"] += comision
            g["neto"] += importe - comision
            if canal == "cortesia":
                tot["cortesias"] += 1
            else:
                tot["boletos"] += 1
                ordenes.add(oid)
                por_dia[_local(pagada).date()] += importe
            tot["venta"] += importe
            tot["iva"] += iva
            tot["comision"] += comision
        else:
            g["cancelados"] += 1
            if estado == EstadoOrden.REEMBOLSADA.value:
                g["reembolsado"] += importe
                tot["reembolsado"] += importe
    renglones = sorted(
        grupos.values(),
        key=lambda r: (r["evento"], r["funcion"], r["zona"], r["tipo"], r["canal"]),
    )
    return Reporte(
        "ventas",
        "Ventas",
        [
            Indicador("Venta bruta", _dinero(tot["venta"]), "dinero"),
            Indicador("Boletos vendidos", int(tot["boletos"])),
            Indicador("Órdenes", len(ordenes)),
            Indicador("Cortesías", int(tot["cortesias"])),
            Indicador("Comisión de PSD", _dinero(tot["comision"]), "dinero"),
            Indicador(
                "Neto del organizador",
                _dinero(tot["venta"] - tot["comision"]),
                "dinero",
            ),
            Indicador("Reembolsado", _dinero(tot["reembolsado"]), "dinero"),
            Indicador(
                "Descuentos por promociones", _dinero(tot["descuento"]), "dinero"
            ),
        ],
        [
            Columna("evento", "Evento"),
            Columna("funcion", "Función", "fechahora"),
            Columna("zona", "Zona"),
            Columna("tipo", "Tipo de boleto"),
            Columna("canal", "Canal"),
            Columna("boletos", "Boletos", "entero"),
            Columna("venta_bruta", "Venta bruta", "dinero"),
            Columna("descuento", "Descuento", "dinero"),
            Columna("iva", "IVA incluido", "dinero"),
            Columna("comision", "Comisión PSD", "dinero"),
            Columna("neto", "Neto organizador", "dinero"),
            Columna("cancelados", "Cancelados", "entero"),
            Columna("reembolsado", "Reembolsado", "dinero"),
        ],
        renglones,
        _por_dia(por_dia),
        [
            "La venta bruta es lo que pagó el comprador, con IVA y ya con el "
            "descuento de las promociones. La comisión de PSD se calcula con la "
            "que tenía cada compra (o cada boleto, si su promoción traía una).",
            "La comisión de Stripe todavía no se guarda por compra: el neto aún "
            "no la descuenta.",
        ],
    )


# --------------------------------------------------------------------------
# Taquilla y cortes
# --------------------------------------------------------------------------


async def _taquilla(db: AsyncSession, f: Filtros) -> Reporte:
    from app.services.taquilla import TaquillaService

    q = (
        select(CajaDeTaquilla, Event.nombre, Venue.zona_horaria, User.nombre)
        .join(Event, Event.id == CajaDeTaquilla.event_id)
        .join(Venue, Venue.id == Event.venue_id)
        .join(User, User.id == CajaDeTaquilla.cajero_id)
    )
    if f.client_id is not None:
        q = q.where(Event.client_id == f.client_id)
    if f.event_id is not None:
        q = q.where(Event.id == f.event_id)
    if f.cajero_id is not None:
        q = q.where(CajaDeTaquilla.cajero_id == f.cajero_id)
    ini, fin = f.rango()
    if f.por == "funcion":
        con_funcion = select(Performance.id).where(Performance.event_id == Event.id)
        if ini is not None:
            con_funcion = con_funcion.where(Performance.inicia_en >= ini)
        if fin is not None:
            con_funcion = con_funcion.where(Performance.inicia_en < fin)
        if ini is not None or fin is not None:
            q = q.where(exists(con_funcion))
    else:
        if ini is not None:
            q = q.where(CajaDeTaquilla.abierta_en >= ini)
        if fin is not None:
            q = q.where(CajaDeTaquilla.abierta_en < fin)
    cajas = (await db.execute(q.order_by(CajaDeTaquilla.abierta_en))).all()

    svc = TaquillaService(db)
    renglones = []
    tot = defaultdict(lambda: CERO)
    for caja, evento, zh, cajero in cajas:
        t = await svc.totales(caja)
        contada = caja.estado in (CONTADA, CERRADA)
        dif_ef = (
            _dinero(caja.efectivo_contado - t.efectivo_esperado) if contada else None
        )
        dif_te = _dinero(caja.terminal_contado - t.terminal) if contada else None
        renglones.append(
            {
                "evento": evento,
                "cajero": cajero,
                "abierta": _local(caja.abierta_en, zh),
                "estado": ESTADOS_CAJA.get(caja.estado, caja.estado),
                "ventas": t.ventas,
                "boletos": t.boletos,
                "cortesias": t.cortesias,
                "efectivo": _dinero(t.efectivo),
                "terminal": _dinero(t.terminal),
                "fondo": _dinero(caja.fondo),
                "efectivo_esperado": _dinero(t.efectivo_esperado),
                "efectivo_contado": _dinero(caja.efectivo_contado) if contada else None,
                "dif_efectivo": dif_ef,
                "terminal_contado": _dinero(caja.terminal_contado) if contada else None,
                "dif_terminal": dif_te,
                "folios": t.folios_entregados,
                "nota": caja.nota_de_cierre,
            }
        )
        tot["ventas"] += t.ventas
        tot["boletos"] += t.boletos
        tot["efectivo"] += t.efectivo
        tot["terminal"] += t.terminal
        if dif_ef is not None:
            tot["diferencia"] += dif_ef + dif_te

    # Quien se queda el efectivo, segun lo que guardo cada venta.
    ids = [c.id for c, *_ in cajas]
    efectivo_de = {"psd": CERO, "organizador": CERO}
    if ids:
        for quien, monto in (
            await db.execute(
                select(
                    VentaDeTaquilla.efectivo_lo_tiene, func.sum(VentaDeTaquilla.total)
                )
                .where(
                    VentaDeTaquilla.caja_id.in_(ids),
                    VentaDeTaquilla.anulada_en.is_(None),
                    VentaDeTaquilla.forma_pago == "efectivo",
                )
                .group_by(VentaDeTaquilla.efectivo_lo_tiene)
            )
        ).all():
            efectivo_de[quien or "organizador"] += monto or CERO
    return Reporte(
        "taquilla",
        "Taquilla y cortes",
        [
            Indicador("Cajas", len(cajas)),
            Indicador("Ventas", int(tot["ventas"])),
            Indicador("Boletos", int(tot["boletos"])),
            Indicador("Efectivo", _dinero(tot["efectivo"]), "dinero"),
            Indicador("Terminal", _dinero(tot["terminal"]), "dinero"),
            Indicador(
                "Efectivo con el organizador",
                _dinero(efectivo_de["organizador"]),
                "dinero",
            ),
            Indicador("Efectivo con PSD", _dinero(efectivo_de["psd"]), "dinero"),
            Indicador("Diferencia en cortes", _dinero(tot["diferencia"]), "dinero"),
        ],
        [
            Columna("evento", "Evento"),
            Columna("cajero", "Cajero"),
            Columna("abierta", "Abierta", "fechahora"),
            Columna("estado", "Estado"),
            Columna("ventas", "Ventas", "entero"),
            Columna("boletos", "Boletos", "entero"),
            Columna("cortesias", "Cortesías", "entero"),
            Columna("efectivo", "Efectivo vendido", "dinero"),
            Columna("terminal", "Terminal vendido", "dinero"),
            Columna("fondo", "Fondo", "dinero"),
            Columna("efectivo_esperado", "Efectivo esperado", "dinero"),
            Columna("efectivo_contado", "Efectivo contado", "dinero"),
            Columna("dif_efectivo", "Diferencia efectivo", "dinero"),
            Columna("terminal_contado", "Terminal contado", "dinero"),
            Columna("dif_terminal", "Diferencia terminal", "dinero"),
            Columna("folios", "Folios entregados", "entero"),
            Columna("nota", "Nota de cierre"),
        ],
        renglones,
        Grafica(
            "Venta por cajero",
            [r["cajero"] for r in renglones],
            [float(r["efectivo"] + r["terminal"]) for r in renglones],
            "dinero",
        )
        if renglones
        else None,
        [
            "Con periodo por compra, cuenta la fecha en que se abrió la caja.",
            "Las diferencias salen cuando el cajero ya contó su caja.",
        ],
    )


# --------------------------------------------------------------------------
# Asistencia
# --------------------------------------------------------------------------


async def _asistencia(db: AsyncSession, f: Filtros) -> Reporte:
    q = (
        select(
            Performance.id,
            Event.nombre,
            Performance.inicia_en,
            Venue.zona_horaria,
            Ticket.estado,
            VentaDeTaquilla.entro_con_medio,
        )
        .join(OrderItem, OrderItem.id == Ticket.order_item_id)
        .join(Order, Order.id == OrderItem.order_id)
        .join(Performance, Performance.id == Order.performance_id)
        .join(Event, Event.id == Performance.event_id)
        .join(Venue, Venue.id == Event.venue_id)
        .outerjoin(VentaDeTaquilla, VentaDeTaquilla.order_id == Order.id)
        .where(Order.estado == EstadoOrden.PAGADA.value, OrderItem.anulado.is_(False))
    )
    q = _periodo(_alcance(q, f), f, Order.pagada_en)
    filas = (await db.execute(q)).all()
    ahora = datetime.now(UTC)
    funciones: dict[int, dict[str, Any]] = {}
    for pid, evento, inicia, zh, estado, con_medio in filas:
        r = funciones.setdefault(
            pid,
            {
                "evento": evento,
                "funcion": _local(inicia, zh),
                "_inicia": inicia,
                "boletos": 0,
                "entraron": 0,
                "con_medio": 0,
            },
        )
        r["boletos"] += 1
        if estado == EstadoBoleto.USADO.value:
            r["entraron"] += 1
            if con_medio:
                r["con_medio"] += 1

    # Entradas por hora: la primera lectura valida de cada boleto usado.
    por_hora: dict[str, int] = defaultdict(int)
    if funciones:
        from app.models.sales import Scan
        from app.services.escaner import VALIDO

        lecturas = (
            await db.execute(
                select(Scan.ticket_id, func.min(Scan.escaneado_en))
                .where(
                    Scan.performance_id.in_(list(funciones)),
                    Scan.resultado == VALIDO,
                    Scan.ticket_id.is_not(None),
                )
                .group_by(Scan.ticket_id)
            )
        ).all()
        for _, momento in lecturas:
            por_hora[_local(momento).strftime("%H:00")] += 1

    renglones = []
    tot = defaultdict(int)
    for r in sorted(funciones.values(), key=lambda x: x["_inicia"]):
        paso = r.pop("_inicia") < ahora
        r["no_llegaron"] = r["boletos"] - r["entraron"] if paso else None
        r["asistencia"] = _pct(r["entraron"], r["boletos"])
        renglones.append(r)
        tot["boletos"] += r["boletos"]
        tot["entraron"] += r["entraron"]
        tot["con_medio"] += r["con_medio"]
    horas = sorted(por_hora)
    return Reporte(
        "asistencia",
        "Asistencia",
        [
            Indicador("Funciones", len(renglones)),
            Indicador("Boletos", tot["boletos"]),
            Indicador("Entraron", tot["entraron"]),
            Indicador("Con medio físico", tot["con_medio"]),
            Indicador("Asistencia", _pct(tot["entraron"], tot["boletos"]), "pct"),
        ],
        [
            Columna("evento", "Evento"),
            Columna("funcion", "Función", "fechahora"),
            Columna("boletos", "Boletos", "entero"),
            Columna("entraron", "Entraron", "entero"),
            Columna("con_medio", "Con medio físico", "entero"),
            Columna("no_llegaron", "No llegaron", "entero"),
            Columna("asistencia", "Asistencia", "pct"),
        ],
        renglones,
        Grafica("Entradas por hora", horas, [por_hora[h] for h in horas])
        if horas
        else None,
        ["«No llegaron» se cuenta cuando la función ya empezó."],
    )


# --------------------------------------------------------------------------
# Cortesias
# --------------------------------------------------------------------------


async def _cortesias(db: AsyncSession, f: Filtros) -> Reporte:
    q = (
        select(
            Cortesia.otorgada_en,
            Cortesia.motivo,
            Cortesia.revocada_en,
            User.nombre,
            Order.folio,
            Order.nombre_snap,
            Event.nombre,
            Performance.inicia_en,
            Venue.zona_horaria,
            OrderItem.zona_snap,
            OrderItem.asiento_snap,
        )
        .join(Order, Order.id == Cortesia.order_id)
        .join(OrderItem, OrderItem.order_id == Order.id)
        .join(Performance, Performance.id == Order.performance_id)
        .join(Event, Event.id == Performance.event_id)
        .join(Venue, Venue.id == Event.venue_id)
        .join(PerformanceSeat, PerformanceSeat.id == OrderItem.performance_seat_id)
        .join(Seat, Seat.id == PerformanceSeat.seat_id)
        .outerjoin(User, User.id == Cortesia.otorgada_por)
    )
    q = _periodo(_alcance(q, f), f, Cortesia.otorgada_en)
    if f.zone_id is not None:
        q = q.where(Seat.zone_id == f.zone_id)
    filas = (
        await db.execute(q.order_by(Cortesia.otorgada_en).limit(MAXIMO_DE_FILAS))
    ).all()
    renglones = []
    por_motivo: dict[str, int] = defaultdict(int)
    vigentes = 0
    for (
        otorgada,
        motivo,
        revocada,
        quien,
        folio,
        para,
        evento,
        inicia,
        zh,
        zona,
        asiento,
    ) in filas:
        renglones.append(
            {
                "otorgada": _local(otorgada, zh),
                "evento": evento,
                "funcion": _local(inicia, zh),
                "zona": zona,
                "asiento": asiento,
                "motivo": motivo,
                "para": para,
                "otorgo": quien,
                "estado": "Revocada" if revocada else "Vigente",
                "folio": folio,
            }
        )
        if not revocada:
            vigentes += 1
            por_motivo[motivo] += 1
    motivos = sorted(por_motivo, key=por_motivo.get, reverse=True)[:8]
    return Reporte(
        "cortesias",
        "Cortesías",
        [
            Indicador("Cortesías vigentes", vigentes),
            Indicador("Revocadas", len(renglones) - vigentes),
            Indicador("Motivos distintos", len(por_motivo)),
        ],
        [
            Columna("otorgada", "Otorgada", "fechahora"),
            Columna("evento", "Evento"),
            Columna("funcion", "Función", "fechahora"),
            Columna("zona", "Zona"),
            Columna("asiento", "Lugar"),
            Columna("motivo", "Motivo"),
            Columna("para", "Para"),
            Columna("otorgo", "La dio"),
            Columna("estado", "Estado"),
            Columna("folio", "Folio"),
        ],
        renglones,
        Grafica("Cortesías por motivo", motivos, [por_motivo[m] for m in motivos])
        if motivos
        else None,
        ["Con periodo por compra, cuenta la fecha en que se dio la cortesía."],
    )


# --------------------------------------------------------------------------
# Compradores
# --------------------------------------------------------------------------


async def _compradores(db: AsyncSession, f: Filtros) -> Reporte:
    q = (
        select(
            Order.customer_id,
            Order.id,
            Order.nombre_snap,
            Order.apellidos_snap,
            Order.email_snap,
            Order.telefono_snap,
            Order.total,
            Order.pagada_en,
            Event.nombre,
            Event.client_id,
            func.count(OrderItem.id),
        )
        .join(OrderItem, OrderItem.order_id == Order.id)
        .join(Performance, Performance.id == Order.performance_id)
        .join(Event, Event.id == Performance.event_id)
        .where(
            Order.estado == EstadoOrden.PAGADA.value,
            OrderItem.anulado.is_(False),
            or_(Order.canal.is_(None), Order.canal != "cortesia"),
        )
        .group_by(Order.id, Event.nombre, Event.client_id)
    )
    q = _periodo(_alcance(q, f), f, Order.pagada_en)
    q = _filtro_canal(q, f.canal if f.canal != "cortesia" else None)
    filas = (await db.execute(q.order_by(Order.pagada_en))).all()

    personas: dict[int, dict[str, Any]] = {}
    clientes_de: dict[int, set[int]] = defaultdict(set)
    for (
        cid,
        _oid,
        nombre,
        apellidos,
        correo,
        tel,
        total,
        pagada,
        evento,
        client,
        n,
    ) in filas:
        p = personas.setdefault(
            cid,
            {
                "nombre": "",
                "correo": "",
                "telefono": "",
                "compras": 0,
                "boletos": 0,
                "total": CERO,
                "primera": _local(pagada),
                "ultima": _local(pagada),
                "_eventos": [],
            },
        )
        # Lo escrito en la compra mas reciente.
        p["nombre"] = " ".join(x for x in (nombre, apellidos) if x) or p["nombre"]
        p["_correo"], p["_telefono"] = correo or "", tel or ""
        p["compras"] += 1
        p["boletos"] += n
        p["total"] += total
        p["ultima"] = _local(pagada)
        if evento not in p["_eventos"]:
            p["_eventos"].append(evento)
        clientes_de[cid].add(client)

    # Acepta promociones: su ultima decision dice que si y alguna vez lo
    # acepto en una compra de este organizador.
    acepta: set[int] = set()
    if personas:
        ultima: dict[int, bool] = {}
        for c, otorgado in (
            await db.execute(
                select(Consent.customer_id, Consent.otorgado)
                .where(
                    Consent.customer_id.in_(list(personas)),
                    Consent.finalidad == "marketing",
                )
                .order_by(Consent.registrado_en, Consent.id)
            )
        ).all():
            ultima[c] = otorgado
        si = {c for c, otorgado in ultima.items() if otorgado}
        if si:
            con_orden = (
                await db.execute(
                    select(Consent.customer_id, Event.client_id)
                    .join(Order, Order.id == Consent.order_id)
                    .join(Performance, Performance.id == Order.performance_id)
                    .join(Event, Event.id == Performance.event_id)
                    .where(
                        Consent.customer_id.in_(list(si)),
                        Consent.finalidad == "marketing",
                        Consent.otorgado.is_(True),
                    )
                )
            ).all()
            for c, client in con_orden:
                if client in clientes_de[c]:
                    acepta.add(c)

    renglones = []
    total = CERO
    compras = 0
    for cid, p in sorted(personas.items(), key=lambda x: x[1]["ultima"], reverse=True):
        si = cid in acepta
        renglones.append(
            {
                "nombre": p["nombre"],
                "acepta": "Sí" if si else "No",
                "correo": p["_correo"] if si else "",
                "telefono": p["_telefono"] if si else "",
                "compras": p["compras"],
                "boletos": p["boletos"],
                "total": _dinero(p["total"]),
                "primera": p["primera"],
                "ultima": p["ultima"],
                "eventos": ", ".join(p["_eventos"]),
            }
        )
        total += p["total"]
        compras += p["compras"]
    return Reporte(
        "compradores",
        "Compradores",
        [
            Indicador("Compradores", len(renglones)),
            Indicador("Aceptan promociones", len(acepta)),
            Indicador("Compras", compras),
            Indicador(
                "Compra promedio",
                _dinero(total / compras) if compras else CERO,
                "dinero",
            ),
        ],
        [
            Columna("nombre", "Nombre"),
            Columna("acepta", "Acepta promociones"),
            Columna("correo", "Correo"),
            Columna("telefono", "Teléfono"),
            Columna("compras", "Compras", "entero"),
            Columna("boletos", "Boletos", "entero"),
            Columna("total", "Total", "dinero"),
            Columna("primera", "Primera compra", "fechahora"),
            Columna("ultima", "Última compra", "fechahora"),
            Columna("eventos", "Eventos"),
        ],
        renglones,
        None,
        [
            "Correo y teléfono salen solo de quien aceptó recibir promociones en "
            "una compra de este organizador. Son los que escribió en su compra.",
            "Cada consulta y descarga de este reporte queda registrada.",
        ],
    )


# --------------------------------------------------------------------------
# Ocupacion
# --------------------------------------------------------------------------


async def _ocupacion(db: AsyncSession, f: Filtros) -> Reporte:
    vivo = and_(
        PerformanceSeat.estado == EstadoAsiento.APARTADO.value,
        PerformanceSeat.hold_expira_en > func.now(),
    )
    cuenta = lambda condicion: func.count(PerformanceSeat.id).filter(condicion)  # noqa: E731
    q = (
        select(
            Event.nombre,
            Performance.inicia_en,
            Venue.zona_horaria,
            Zone.nombre,
            func.count(PerformanceSeat.id),
            cuenta(PerformanceSeat.estado == EstadoAsiento.VENDIDO.value),
            cuenta(PerformanceSeat.estado == EstadoAsiento.CORTESIA.value),
            cuenta(vivo),
            cuenta(PerformanceSeat.estado == EstadoAsiento.BLOQUEADO.value),
        )
        .join(Performance, Performance.id == PerformanceSeat.performance_id)
        .join(Event, Event.id == Performance.event_id)
        .join(Venue, Venue.id == Event.venue_id)
        .join(Seat, Seat.id == PerformanceSeat.seat_id)
        .join(Zone, Zone.id == Seat.zone_id)
        .where(PerformanceSeat.estado != EstadoAsiento.KILL.value)
        .group_by(Event.nombre, Performance.id, Venue.zona_horaria, Zone.id)
    )
    q = _alcance(q, f)
    ini, fin = f.rango()
    if ini is not None:
        q = q.where(Performance.inicia_en >= ini)
    if fin is not None:
        q = q.where(Performance.inicia_en < fin)
    if f.zone_id is not None:
        q = q.where(Zone.id == f.zone_id)
    filas = (
        await db.execute(q.order_by(Performance.inicia_en, Zone.orden, Zone.id))
    ).all()
    renglones = []
    tot = defaultdict(int)
    por_funcion: dict[str, list[int]] = {}
    for evento, inicia, zh, zona, cap, vend, cort, apart, bloq in filas:
        ocupados = vend + cort
        renglones.append(
            {
                "evento": evento,
                "funcion": _local(inicia, zh),
                "zona": zona,
                "capacidad": cap,
                "vendidos": vend,
                "cortesias": cort,
                "apartados": apart,
                "bloqueados": bloq,
                "disponibles": cap - ocupados - apart - bloq,
                "ocupacion": _pct(ocupados, cap),
            }
        )
        etiqueta = f"{evento[:24]} · {_local(inicia, zh):%d/%m %H:%M}"
        p = por_funcion.setdefault(etiqueta, [0, 0])
        p[0] += ocupados
        p[1] += cap
        for k, v in (("cap", cap), ("vend", vend), ("cort", cort), ("bloq", bloq)):
            tot[k] += v
    funciones = list(por_funcion)[:20]
    return Reporte(
        "ocupacion",
        "Ocupación",
        [
            Indicador("Capacidad", tot["cap"]),
            Indicador("Vendidos", tot["vend"]),
            Indicador("Cortesías", tot["cort"]),
            Indicador("Bloqueados", tot["bloq"]),
            Indicador("Ocupación", _pct(tot["vend"] + tot["cort"], tot["cap"]), "pct"),
        ],
        [
            Columna("evento", "Evento"),
            Columna("funcion", "Función", "fechahora"),
            Columna("zona", "Zona"),
            Columna("capacidad", "Capacidad", "entero"),
            Columna("vendidos", "Vendidos", "entero"),
            Columna("cortesias", "Cortesías", "entero"),
            Columna("apartados", "Apartados ahora", "entero"),
            Columna("bloqueados", "Bloqueados", "entero"),
            Columna("disponibles", "Disponibles", "entero"),
            Columna("ocupacion", "Ocupación", "pct"),
        ],
        renglones,
        Grafica(
            "Ocupación por función",
            funciones,
            [float(_pct(*por_funcion[x]) or 0) for x in funciones],
            "pct",
        )
        if funciones
        else None,
        [
            "Ocupación = vendidos + cortesías sobre la capacidad. Siempre por "
            "fecha de la función."
        ],
    )


# --------------------------------------------------------------------------
# Promociones (PR1)
# --------------------------------------------------------------------------


async def _promociones(db: AsyncSession, f: Filtros) -> Reporte:
    q = (
        select(
            Order.promocion_snap,
            Event.nombre,
            Performance.inicia_en,
            Venue.zona_horaria,
            Order.canal,
            Order.estado,
            OrderItem.anulado,
            OrderItem.importe,
            OrderItem.descuento,
            func.coalesce(OrderItem.comision_pct_snap, Order.comision_pct_snap),
            Order.id,
        )
        .join(Order, Order.id == OrderItem.order_id)
        .join(Performance, Performance.id == Order.performance_id)
        .join(Event, Event.id == Performance.event_id)
        .join(Venue, Venue.id == Event.venue_id)
        .where(
            Order.promocion_id.is_not(None),
            Order.estado.in_(VENDIDAS),
            Order.pagada_en.is_not(None),
        )
    )
    q = _filtro_canal(_periodo(_alcance(q, f), f, Order.pagada_en), f.canal)
    filas = (await db.execute(q.limit(MAXIMO_DE_FILAS))).all()

    grupos: dict[tuple, dict[str, Any]] = {}
    ordenes: set[int] = set()
    por_promo: dict[str, Decimal] = defaultdict(lambda: CERO)
    tot = defaultdict(lambda: CERO)
    for (
        promo,
        evento,
        inicia,
        zh,
        canal_orden,
        estado,
        anulado,
        importe,
        descuento,
        pct,
        oid,
    ) in filas:
        canal = _canal(canal_orden)
        g = grupos.setdefault(
            (promo, evento, inicia, canal),
            {
                "promocion": promo,
                "evento": evento,
                "funcion": _local(inicia, zh),
                "canal": CANALES[canal],
                "compras": set(),
                "boletos": 0,
                "sin_costo": 0,
                "venta": CERO,
                "descuento": CERO,
                "comision": CERO,
                "neto": CERO,
                "cancelados": 0,
            },
        )
        if estado == EstadoOrden.PAGADA.value and not anulado:
            comision = _dinero(importe * pct / 100)
            g["compras"].add(oid)
            g["boletos"] += 1
            if importe == 0 and descuento > 0:
                g["sin_costo"] += 1
            g["venta"] += importe
            g["descuento"] += descuento
            g["comision"] += comision
            g["neto"] += importe - comision
            ordenes.add(oid)
            por_promo[promo] += importe
            tot["boletos"] += 1
            tot["venta"] += importe
            tot["descuento"] += descuento
            tot["comision"] += comision
        else:
            g["cancelados"] += 1
    renglones = []
    for g in sorted(
        grupos.values(),
        key=lambda r: (r["promocion"], r["evento"], r["funcion"], r["canal"]),
    ):
        renglones.append({**g, "compras": len(g["compras"])})
    grafica = (
        Grafica(
            "Venta por promoción",
            list(por_promo),
            [float(v) for v in por_promo.values()],
            "dinero",
        )
        if por_promo
        else None
    )
    return Reporte(
        "promociones",
        "Promociones",
        [
            Indicador("Venta con promoción", _dinero(tot["venta"]), "dinero"),
            Indicador("Compras con promoción", len(ordenes)),
            Indicador("Boletos", int(tot["boletos"])),
            Indicador("Descontado", _dinero(tot["descuento"]), "dinero"),
            Indicador("Comisión de PSD", _dinero(tot["comision"]), "dinero"),
            Indicador(
                "Neto del organizador",
                _dinero(tot["venta"] - tot["comision"]),
                "dinero",
            ),
        ],
        [
            Columna("promocion", "Promoción"),
            Columna("evento", "Evento"),
            Columna("funcion", "Función", "fechahora"),
            Columna("canal", "Canal"),
            Columna("compras", "Compras", "entero"),
            Columna("boletos", "Boletos", "entero"),
            Columna("sin_costo", "Sin costo (2x1)", "entero"),
            Columna("venta", "Venta", "dinero"),
            Columna("descuento", "Descontado", "dinero"),
            Columna("comision", "Comisión PSD", "dinero"),
            Columna("neto", "Neto organizador", "dinero"),
            Columna("cancelados", "Cancelados", "entero"),
        ],
        renglones,
        grafica,
        [
            "Solo compras con promoción. La venta es lo que pagó el comprador, ya "
            "con el descuento; lo descontado es lo que dejó de cobrarse.",
            "El nombre es el que tenía la promoción al momento de cada compra.",
        ],
    )


GENERADORES = {
    "ventas": _ventas,
    "taquilla": _taquilla,
    "asistencia": _asistencia,
    "cortesias": _cortesias,
    "compradores": _compradores,
    "ocupacion": _ocupacion,
    "promociones": _promociones,
}


async def generar(db: AsyncSession, tipo: str, f: Filtros) -> Reporte:
    return await GENERADORES[tipo](db, f)


# --------------------------------------------------------------------------
# Opciones de los filtros
# --------------------------------------------------------------------------


async def opciones(
    db: AsyncSession, usuario: User, client_id: int | None, event_id: int | None
) -> dict[str, list[dict[str, Any]]]:
    """Organizadores (PSD), eventos y, con un evento, sus funciones, zonas y
    cajeros. Solo lo que quien pregunta puede ver."""
    from app.models.catalog import Client

    if usuario.rol == RolUsuario.ORGANIZADOR:
        client_id = usuario.client_id
    jefe = None
    if usuario.rol == RolUsuario.ACCESO:
        jefe = await eventos_de_jefe(db, usuario)
        client_id = usuario.client_id
    organizadores = []
    if usuario.rol == RolUsuario.ADMIN_PSD:
        organizadores = [
            {"id": i, "nombre": n}
            for i, n in (
                await db.execute(
                    select(Client.id, Client.nombre).order_by(Client.nombre)
                )
            ).all()
        ]
    qe = select(Event.id, Event.nombre).order_by(Event.nombre)
    if client_id is not None:
        qe = qe.where(Event.client_id == client_id)
    if jefe is not None:
        qe = qe.where(Event.id.in_(jefe or [-1]))
    eventos = [{"id": i, "nombre": n} for i, n in (await db.execute(qe)).all()]
    funciones, zonas, cajeros = [], [], []
    if event_id is not None and event_id in {e["id"] for e in eventos}:
        tz = await db.scalar(
            select(Venue.zona_horaria)
            .join(Event, Event.venue_id == Venue.id)
            .where(Event.id == event_id)
        )
        funciones = [
            {"id": i, "inicia_en": _local(ini, tz).isoformat()}
            for i, ini in (
                await db.execute(
                    select(Performance.id, Performance.inicia_en)
                    .where(Performance.event_id == event_id)
                    .order_by(Performance.inicia_en)
                )
            ).all()
        ]
        zonas = [
            {"id": i, "nombre": n}
            for i, n in (
                await db.execute(
                    select(Zone.id, Zone.nombre)
                    .where(
                        Zone.seat_map_id.in_(
                            select(Performance.seat_map_id).where(
                                Performance.event_id == event_id
                            )
                        )
                    )
                    .order_by(Zone.orden, Zone.id)
                )
            ).all()
        ]
        cajeros = [
            {"id": i, "nombre": n}
            for i, n in (
                await db.execute(
                    select(User.id, User.nombre)
                    .where(
                        User.id.in_(
                            select(CajaDeTaquilla.cajero_id).where(
                                CajaDeTaquilla.event_id == event_id
                            )
                        )
                    )
                    .order_by(User.nombre)
                )
            ).all()
        ]
    return {
        "organizadores": organizadores,
        "eventos": eventos,
        "funciones": funciones,
        "zonas": zonas,
        "cajeros": cajeros,
    }


__all__ = [
    "CATALOGO",
    "Customer",
    "Filtros",
    "Reporte",
    "ReporteInvalido",
    "acotar",
    "disponibles",
    "generar",
    "opciones",
    "registrar",
]
