"""Metricas del panel (bloque 14): solo lectura, sin tablas nuevas.

Cada numero va junto a la decision que ayuda a tomar:

- Eventos: proxima funcion, vendidos de cuantos lugares, venta y como va.
- Funciones de un evento: lo mismo por funcion, y cuantos dias faltan.
- Una funcion: vendidos por zona (junto al precio), por donde y a quien se
  vende, venta por dia, y la puerta: cuantos entraron y, al terminar, como
  llegaron (cada 15 minutos).

Administracion (bloque 15):

- Recintos: eventos, funciones, ocupacion promedio de las que ya pasaron y
  proxima funcion. El recinto es de todos; el organizador solo cuenta lo suyo.
- Organizadores (solo PSD): venta y comision de PSD del mes.
- Cuentas: cuantos eventos tiene asignados y lo que escaneo en su ultima
  funcion.

Quien ve que lo decide quien llama: PSD todo; el organizador solo lo suyo
(`client_id`); el personal de acceso no tiene estas pantallas.

"Vendidos" son los lugares vendidos (sin cortesias) y "venta" lo que se
cobro (`order_items.importe`, ya con descuento e IVA), sin cortesias.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.acceso import AsignacionDeAcceso
from app.models.base import (
    EstadoAsiento,
    EstadoBoleto,
    EstadoEvento,
    EstadoFuncion,
    EstadoOrden,
)
from app.models.catalog import Client, Event, Performance, Seat, SeatMap, Venue, Zone
from app.models.inventory import PerformanceSeat
from app.models.sales import Order, OrderItem, Scan, Ticket
from app.models.taquilla import VentaDeTaquilla
from app.models.users import User
from app.services.escaner import DESHECHO, FUERA_DE_HORARIO, VALIDO, horario_de_escaneo
from app.services.reportes import _canal, _dinero, _local, _pct

CERO = Decimal("0.00")
# Una funcion "va floja" si faltan pocos dias y lleva poco vendido.
DIAS_PARA_AVISAR = 3
FLOJA = Decimal("0.30")
CASI_AGOTADO = Decimal("0.90")
CUARTO = timedelta(minutes=15)
CENTRO = ZoneInfo("America/Mexico_City")


def _venta_viva():
    """Partidas pagadas, vivas y que no son cortesia."""
    return (
        Order.estado == EstadoOrden.PAGADA.value,
        OrderItem.anulado.is_(False),
        or_(Order.canal.is_(None), Order.canal != "cortesia"),
    )


async def _conteos(db: AsyncSession, funciones: list[int]) -> dict[int, dict[str, int]]:
    """Lugares por funcion: capacidad (sin los inutilizables), vendidos,
    cortesias y libres."""
    salida: dict[int, dict[str, int]] = {}
    if not funciones:
        return salida
    filas = await db.execute(
        select(
            PerformanceSeat.performance_id,
            func.count(PerformanceSeat.id),
            func.count(PerformanceSeat.id).filter(
                PerformanceSeat.estado == EstadoAsiento.VENDIDO.value
            ),
            func.count(PerformanceSeat.id).filter(
                PerformanceSeat.estado == EstadoAsiento.CORTESIA.value
            ),
            func.count(PerformanceSeat.id).filter(
                PerformanceSeat.estado == EstadoAsiento.DISPONIBLE.value
            ),
        )
        .where(
            PerformanceSeat.performance_id.in_(funciones),
            PerformanceSeat.estado != EstadoAsiento.KILL.value,
        )
        .group_by(PerformanceSeat.performance_id)
    )
    for pid, cap, vend, cort, libres in filas.all():
        salida[pid] = {
            "capacidad": int(cap),
            "vendidos": int(vend),
            "cortesias": int(cort),
            "libres": int(libres),
        }
    return salida


async def _ventas(db: AsyncSession, funciones: list[int]) -> dict[int, Decimal]:
    if not funciones:
        return {}
    filas = await db.execute(
        select(Order.performance_id, func.coalesce(func.sum(OrderItem.importe), 0))
        .join(Order, Order.id == OrderItem.order_id)
        .where(Order.performance_id.in_(funciones), *_venta_viva())
        .group_by(Order.performance_id)
    )
    return {pid: _dinero(m) for pid, m in filas.all()}


def _como_va(
    evento_estado: str,
    funcion: Performance | None,
    capacidad: int,
    vendidos: int,
    dias: int | None,
) -> tuple[str, str]:
    """(tono, texto) de una funcion o de un evento por su proxima funcion."""
    if evento_estado == EstadoEvento.BORRADOR.value:
        return "mute", "Borrador"
    if funcion is None:
        return "mute", "Sin funciones próximas"
    if funcion.estado == EstadoFuncion.CANCELADA.value:
        return "bad", "Cancelada"
    if not funcion.inventario_generado or not capacidad:
        return "mute", "Por preparar"
    if dias is not None and dias < 0:
        return "mute", "Terminada"
    avance = Decimal(vendidos) / Decimal(capacidad)
    if funcion.estado == EstadoFuncion.AGOTADA.value or avance >= 1:
        return "bad", "Agotada"
    if avance >= CASI_AGOTADO:
        return "bad", "Casi agotada"
    if funcion.estado != EstadoFuncion.EN_VENTA.value:
        return "mute", "Sin venta"
    if dias is not None and dias <= DIAS_PARA_AVISAR and avance < FLOJA:
        return "warn", "Venta lenta"
    return "good", "En venta"


def _dias(funcion: Performance, zona_horaria: str, hoy: date | None = None) -> int:
    """Dias del hoy del recinto al dia de la funcion (0: hoy; negativo: ya paso)."""
    zona = ZoneInfo(zona_horaria)
    hoy = hoy or datetime.now(zona).date()
    return (funcion.inicia_en.astimezone(zona).date() - hoy).days


def dias_en_texto(dias: int) -> str:
    if dias < 0:
        return "Ya pasó"
    if dias == 0:
        return "Hoy"
    if dias == 1:
        return "Mañana"
    return f"Faltan {dias} días"


# --------------------------------------------------------------------------
# Eventos
# --------------------------------------------------------------------------


async def eventos(db: AsyncSession, client_id: int | None) -> list[dict[str, Any]]:
    """Por evento: proxima funcion, vendidos de cuantos (funciones que no se
    han cancelado), venta y como va."""
    q = (
        select(Event, Venue)
        .join(Venue, Venue.id == Event.venue_id)
        .where(Event.estado != EstadoEvento.ARCHIVADO.value)
    )
    if client_id is not None:
        q = q.where(Event.client_id == client_id)
    eventos_ = (await db.execute(q)).all()
    if not eventos_:
        return []
    ids = [e.id for e, _ in eventos_]
    funciones = (
        await db.scalars(
            select(Performance)
            .where(
                Performance.event_id.in_(ids),
                Performance.estado != EstadoFuncion.CANCELADA.value,
            )
            .order_by(Performance.inicia_en)
        )
    ).all()
    por_evento: dict[int, list[Performance]] = defaultdict(list)
    for f in funciones:
        por_evento[f.event_id].append(f)
    conteos = await _conteos(db, [f.id for f in funciones])
    ventas = await _ventas(db, [f.id for f in funciones])
    ahora = datetime.now(UTC)
    salida = []
    for e, v in eventos_:
        fs = por_evento.get(e.id, [])
        cap = sum(conteos.get(f.id, {}).get("capacidad", 0) for f in fs)
        vend = sum(conteos.get(f.id, {}).get("vendidos", 0) for f in fs)
        venta = sum((ventas.get(f.id, CERO) for f in fs), CERO)
        proxima = next((f for f in fs if f.inicia_en >= ahora - CUARTO * 16), None)
        c = conteos.get(proxima.id, {}) if proxima else {}
        dias = _dias(proxima, v.zona_horaria) if proxima else None
        tono, texto = _como_va(
            e.estado, proxima, c.get("capacidad", 0), c.get("vendidos", 0), dias
        )
        salida.append(
            {
                "event_id": e.id,
                "proxima": None
                if proxima is None
                else {
                    "performance_id": proxima.id,
                    "inicia_en": _local(proxima.inicia_en, v.zona_horaria).isoformat(),
                    "dias": dias,
                },
                "funciones": len(fs),
                "capacidad": cap,
                "vendidos": vend,
                "venta": str(venta),
                "tono": tono,
                "estado": texto,
            }
        )
    return salida


async def funciones_de_evento(
    db: AsyncSession, evento: Event, venue: Venue
) -> list[dict[str, Any]]:
    fs = (
        await db.scalars(
            select(Performance)
            .where(Performance.event_id == evento.id)
            .order_by(Performance.inicia_en)
        )
    ).all()
    conteos = await _conteos(db, [f.id for f in fs])
    ventas = await _ventas(db, [f.id for f in fs])
    salida = []
    for f in fs:
        c = conteos.get(f.id, {})
        dias = _dias(f, venue.zona_horaria)
        tono, texto = _como_va(
            evento.estado, f, c.get("capacidad", 0), c.get("vendidos", 0), dias
        )
        salida.append(
            {
                "performance_id": f.id,
                "inicia_en": _local(f.inicia_en, venue.zona_horaria).isoformat(),
                "apertura_puertas": _local(
                    f.apertura_puertas, venue.zona_horaria
                ).isoformat()
                if f.apertura_puertas
                else None,
                "capacidad": c.get("capacidad", 0),
                "vendidos": c.get("vendidos", 0),
                "cortesias": c.get("cortesias", 0),
                "venta": str(ventas.get(f.id, CERO)),
                "dias": dias,
                "dias_texto": dias_en_texto(dias),
                "floja": tono == "warn" and texto == "Venta lenta",
                "tono": tono,
                "estado": texto,
            }
        )
    return salida


# --------------------------------------------------------------------------
# Una funcion
# --------------------------------------------------------------------------


async def funcion(
    db: AsyncSession, f: Performance, evento: Event, venue: Venue
) -> dict[str, Any]:
    zh = venue.zona_horaria
    conteo = (await _conteos(db, [f.id])).get(
        f.id, {"capacidad": 0, "vendidos": 0, "cortesias": 0, "libres": 0}
    )
    venta = (await _ventas(db, [f.id])).get(f.id, CERO)

    # Por zona: junto al precio, cuanto lleva vendido.
    zonas = []
    for zid, nombre, cap, vend, cort, libres in (
        await db.execute(
            select(
                Zone.id,
                Zone.nombre,
                func.count(PerformanceSeat.id),
                func.count(PerformanceSeat.id).filter(
                    PerformanceSeat.estado == EstadoAsiento.VENDIDO.value
                ),
                func.count(PerformanceSeat.id).filter(
                    PerformanceSeat.estado == EstadoAsiento.CORTESIA.value
                ),
                func.count(PerformanceSeat.id).filter(
                    PerformanceSeat.estado == EstadoAsiento.DISPONIBLE.value
                ),
            )
            .select_from(PerformanceSeat)
            .join(Seat, Seat.id == PerformanceSeat.seat_id)
            .join(Zone, Zone.id == Seat.zone_id)
            .where(
                PerformanceSeat.performance_id == f.id,
                PerformanceSeat.estado != EstadoAsiento.KILL.value,
            )
            .group_by(Zone.id, Zone.nombre, Zone.orden)
            .order_by(Zone.orden, Zone.id)
        )
    ).all():
        zonas.append(
            {
                "zone_id": zid,
                "nombre": nombre,
                "capacidad": int(cap),
                "vendidos": int(vend),
                "cortesias": int(cort),
                "libres": int(libres),
            }
        )

    # Por donde y a quien: partidas vivas pagadas (las cortesias aparte).
    canales = {
        "linea": {"boletos": 0, "monto": CERO},
        "taquilla": {"boletos": 0, "monto": CERO},
    }
    tipos = {"adulto": 0, "nino": 0, "mayor": 0}
    por_dia: dict[date, Decimal] = defaultdict(lambda: CERO)
    for canal, tipo, importe, pagada in (
        await db.execute(
            select(Order.canal, OrderItem.tipo, OrderItem.importe, Order.pagada_en)
            .join(Order, Order.id == OrderItem.order_id)
            .where(Order.performance_id == f.id, *_venta_viva())
        )
    ).all():
        c = _canal(canal)
        canales[c]["boletos"] += 1
        canales[c]["monto"] += importe
        tipos[tipo] = tipos.get(tipo, 0) + 1
        if pagada is not None:
            por_dia[_local(pagada, zh).date()] += importe
    hoy = datetime.now(ZoneInfo(zh)).date()
    ultimo = min(hoy, f.inicia_en.astimezone(ZoneInfo(zh)).date())
    dias = [ultimo - timedelta(days=i) for i in range(6, -1, -1)]

    return {
        "performance_id": f.id,
        "event_id": evento.id,
        "evento": evento.nombre,
        "recinto": venue.nombre,
        "ciudad": venue.ciudad,
        "zona_horaria": zh,
        "inicia_en": _local(f.inicia_en, zh).isoformat(),
        "apertura_puertas": _local(f.apertura_puertas, zh).isoformat()
        if f.apertura_puertas
        else None,
        "estado_funcion": f.estado,
        "inventario_generado": f.inventario_generado,
        "dias": _dias(f, zh),
        "capacidad": conteo["capacidad"],
        "vendidos": conteo["vendidos"],
        "cortesias": conteo["cortesias"],
        "disponibles": conteo["libres"],
        "venta": str(venta),
        "zonas": zonas,
        "canales": {
            k: {"boletos": v["boletos"], "monto": str(_dinero(v["monto"]))}
            for k, v in canales.items()
        },
        "tipos": tipos,
        "venta_por_dia": [
            {"dia": d.isoformat(), "monto": str(_dinero(por_dia[d]))} for d in dias
        ],
        "puerta": await _puerta(db, f, evento, zh),
    }


async def _puerta(
    db: AsyncSession, f: Performance, evento: Event, zh: str
) -> dict[str, Any]:
    """Cuantos entraron y, ya terminada, como llegaron cada 15 minutos."""
    boletos = entraron = con_medio = 0
    for estado, medio in (
        await db.execute(
            select(Ticket.estado, VentaDeTaquilla.entro_con_medio)
            .join(OrderItem, OrderItem.id == Ticket.order_item_id)
            .join(Order, Order.id == OrderItem.order_id)
            .outerjoin(VentaDeTaquilla, VentaDeTaquilla.order_id == Order.id)
            .where(
                Order.performance_id == f.id,
                Order.estado == EstadoOrden.PAGADA.value,
                OrderItem.anulado.is_(False),
                Ticket.estado != EstadoBoleto.CANCELADO.value,
            )
        )
    ).all():
        boletos += 1
        if estado == EstadoBoleto.USADO.value:
            entraron += 1
            if medio:
                con_medio += 1
    rechazos = await db.scalar(
        select(func.count(Scan.id)).where(
            Scan.performance_id == f.id,
            Scan.resultado.not_in((VALIDO, DESHECHO, FUERA_DE_HORARIO)),
        )
    )
    abre, cierra = horario_de_escaneo(f)
    ahora = datetime.now(UTC)
    termino = ahora >= cierra
    llegadas: list[dict[str, Any]] = []
    pico = None
    if termino:
        primeras = [
            m
            for (m,) in (
                await db.execute(
                    select(func.min(Scan.escaneado_en))
                    .where(
                        Scan.performance_id == f.id,
                        Scan.resultado == VALIDO,
                        Scan.ticket_id.is_not(None),
                    )
                    .group_by(Scan.ticket_id)
                )
            ).all()
        ]
        if primeras:
            cuenta: dict[datetime, int] = defaultdict(int)
            for m in primeras:
                local = _local(m, zh)
                cuenta[
                    local.replace(
                        minute=local.minute // 15 * 15, second=0, microsecond=0
                    )
                ] += 1
            t, fin = min(cuenta), max(cuenta)
            while t <= fin:
                llegadas.append(
                    {"hora": t.strftime("%H:%M"), "entraron": cuenta.get(t, 0)}
                )
                t += CUARTO
            mayor = max(llegadas, key=lambda x: x["entraron"])
            pico = mayor["hora"]
    personal = [
        n
        for (n,) in (
            await db.execute(
                select(User.nombre)
                .join(AsignacionDeAcceso, AsignacionDeAcceso.user_id == User.id)
                .where(AsignacionDeAcceso.event_id == evento.id, User.activo.is_(True))
                .order_by(User.nombre)
            )
        ).all()
    ]
    return {
        "boletos": boletos,
        "entraron": entraron,
        "con_medio": con_medio,
        "rechazos": int(rechazos or 0),
        "no_llegaron": boletos - entraron if termino else None,
        "abre": _local(abre, zh).isoformat(),
        "cierra": _local(cierra, zh).isoformat(),
        "termino": termino,
        "llegadas": llegadas,
        "pico": pico,
        "personal": personal,
    }


# --------------------------------------------------------------------------
# Promociones: lo del mes
# --------------------------------------------------------------------------


async def promocion_del_mes(db: AsyncSession, promocion_id: int) -> dict[str, Any]:
    """Boletos, descontado y vendido este mes (por fecha de compra, hora del
    centro de Mexico) con esa promocion."""
    inicio = _inicio_del_mes()
    boletos, descontado, vendido = (
        await db.execute(
            select(
                func.count(OrderItem.id),
                func.coalesce(func.sum(OrderItem.descuento), 0),
                func.coalesce(func.sum(OrderItem.importe), 0),
            )
            .join(Order, Order.id == OrderItem.order_id)
            .where(
                Order.promocion_id == promocion_id,
                Order.estado == EstadoOrden.PAGADA.value,
                OrderItem.anulado.is_(False),
                Order.pagada_en >= inicio,
            )
        )
    ).one()
    return {
        "boletos": int(boletos),
        "descontado": str(_dinero(descontado)),
        "vendido": str(_dinero(vendido)),
    }


def _inicio_del_mes(ahora: datetime | None = None) -> datetime:
    """Dia 1 a las 00:00 en hora del centro, como instante UTC: lo mismo que
    cuenta el reporte de Ventas por fecha de compra."""
    hoy = (ahora or datetime.now(UTC)).astimezone(CENTRO)
    return hoy.replace(day=1, hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)


def _rechazo():
    """Lecturas que la puerta no dejo pasar (no cuentan las deshechas ni las
    de fuera de horario, que no son del boleto)."""
    return Scan.resultado.not_in((VALIDO, DESHECHO, FUERA_DE_HORARIO))


# --------------------------------------------------------------------------
# Recintos (bloque 15)
# --------------------------------------------------------------------------


async def _por_recinto(
    db: AsyncSession, client_id: int | None, venue_id: int | None = None
) -> dict[int, dict[str, Any]]:
    """Eventos, funciones, ocupacion promedio y proxima funcion por recinto.

    La ocupacion es (vendidos + cortesias) / lugares, como el reporte de
    Ocupacion, promediada entre las funciones que ya pasaron: las que vienen
    todavia se estan vendiendo y la bajarian sin decir nada."""
    q = (
        select(Event.venue_id, Event.id, Performance)
        .join(Performance, Performance.event_id == Event.id, isouter=True)
        .where(Event.estado != EstadoEvento.ARCHIVADO.value)
    )
    if client_id is not None:
        q = q.where(Event.client_id == client_id)
    if venue_id is not None:
        q = q.where(Event.venue_id == venue_id)
    filas = (await db.execute(q)).all()
    vivas = [
        f
        for _, _, f in filas
        if f is not None and f.estado != EstadoFuncion.CANCELADA.value
    ]
    ahora = datetime.now(UTC)
    pasadas = [f for f in vivas if f.inicia_en < ahora]
    conteos = await _conteos(db, [f.id for f in pasadas if f.inventario_generado])
    salida: dict[int, dict[str, Any]] = {}
    eventos_con_proxima: dict[int, set[int]] = defaultdict(set)
    for vid, eid, f in filas:
        r = salida.setdefault(
            vid,
            {
                "eventos": set(),
                "funciones": 0,
                "pasadas": 0,
                "ocupaciones": [],
                "proxima": None,
            },
        )
        r["eventos"].add(eid)
        if f is None or f.estado == EstadoFuncion.CANCELADA.value:
            continue
        r["funciones"] += 1
        if f.inicia_en < ahora:
            r["pasadas"] += 1
            c = conteos.get(f.id)
            if c and c["capacidad"]:
                r["ocupaciones"].append(
                    _pct(c["vendidos"] + c["cortesias"], c["capacidad"])
                )
        else:
            eventos_con_proxima[vid].add(eid)
            if r["proxima"] is None or f.inicia_en < r["proxima"]:
                r["proxima"] = f.inicia_en
    for vid, r in salida.items():
        ocup = r.pop("ocupaciones")
        r["ocupacion"] = (
            str((sum(ocup) / len(ocup)).quantize(Decimal("0.1"))) if ocup else None
        )
        r["eventos_proximos"] = len(eventos_con_proxima[vid])
        r["eventos"] = len(r["eventos"])
    return salida


def _recinto(v: Venue, mapas: int, datos: dict[str, Any] | None) -> dict[str, Any]:
    d = datos or {}
    return {
        "venue_id": v.id,
        "mapas": mapas,
        "eventos": d.get("eventos", 0),
        "eventos_proximos": d.get("eventos_proximos", 0),
        "funciones": d.get("funciones", 0),
        "funciones_pasadas": d.get("pasadas", 0),
        "ocupacion": d.get("ocupacion"),
        "proxima": _local(d["proxima"], v.zona_horaria).isoformat()
        if d.get("proxima")
        else None,
        "con_ubicacion": v.lat is not None and v.lng is not None,
    }


async def recintos(db: AsyncSession, client_id: int | None) -> list[dict[str, Any]]:
    """Todos los recintos (son de todos); los numeros, de quien pregunta."""
    venues = (await db.scalars(select(Venue))).all()
    mapas = dict(
        (
            await db.execute(
                select(SeatMap.venue_id, func.count(SeatMap.id)).group_by(
                    SeatMap.venue_id
                )
            )
        ).all()
    )
    datos = await _por_recinto(db, client_id)
    return [_recinto(v, int(mapas.get(v.id, 0)), datos.get(v.id)) for v in venues]


async def recinto(db: AsyncSession, v: Venue, client_id: int | None) -> dict[str, Any]:
    """Un recinto y, por mapa, en cuantas funciones se usa. `en_uso` cuenta las
    de todos: un mapa con funciones ya no se cambia, sean de quien sean."""
    datos = (await _por_recinto(db, client_id, v.id)).get(v.id)
    por_mapa: dict[int, dict[str, Any]] = {}
    q = (
        select(
            SeatMap.id,
            func.count(Performance.id),
            func.count(Performance.id).filter(Event.client_id == client_id)
            if client_id is not None
            else func.count(Performance.id),
        )
        .select_from(SeatMap)
        .outerjoin(Performance, Performance.seat_map_id == SeatMap.id)
        .outerjoin(Event, Event.id == Performance.event_id)
        .where(SeatMap.venue_id == v.id)
        .group_by(SeatMap.id)
    )
    for mid, todas, mias in (await db.execute(q)).all():
        por_mapa[mid] = {
            "seat_map_id": mid,
            "funciones": int(mias),
            "en_uso": int(todas) > 0,
        }
    return {
        **_recinto(v, len(por_mapa), datos),
        "mapas_uso": sorted(por_mapa.values(), key=lambda m: m["seat_map_id"]),
    }


# --------------------------------------------------------------------------
# Organizadores (bloque 15, solo PSD)
# --------------------------------------------------------------------------


async def organizadores(db: AsyncSession) -> dict[str, Any]:
    """Venta del mes (lo cobrado, sin cortesias, por fecha de compra) y la
    comision de PSD de esa venta, con la comision congelada en cada boleto
    y redondeada por boleto, igual que el reporte de Ventas."""
    inicio = _inicio_del_mes()
    venta: dict[int, Decimal] = defaultdict(lambda: CERO)
    comision: dict[int, Decimal] = defaultdict(lambda: CERO)
    for cid, importe, pct in (
        await db.execute(
            select(
                Event.client_id,
                OrderItem.importe,
                func.coalesce(OrderItem.comision_pct_snap, Order.comision_pct_snap),
            )
            .join(Order, Order.id == OrderItem.order_id)
            .join(Performance, Performance.id == Order.performance_id)
            .join(Event, Event.id == Performance.event_id)
            .where(*_venta_viva(), Order.pagada_en >= inicio)
        )
    ).all():
        venta[cid] += importe
        comision[cid] += _dinero(importe * (pct or 0) / 100)
    eventos = dict(
        (
            await db.execute(
                select(Event.client_id, func.count(Event.id))
                .where(Event.estado != EstadoEvento.ARCHIVADO.value)
                .group_by(Event.client_id)
            )
        ).all()
    )
    cuentas = dict(
        (
            await db.execute(
                select(User.client_id, func.count(User.id))
                .where(User.client_id.is_not(None), User.activo.is_(True))
                .group_by(User.client_id)
            )
        ).all()
    )
    clientes = (await db.scalars(select(Client))).all()
    filas = [
        {
            "client_id": c.id,
            "eventos": int(eventos.get(c.id, 0)),
            "cuentas": int(cuentas.get(c.id, 0)),
            "venta_mes": str(_dinero(venta[c.id])),
            "comision_mes": str(_dinero(comision[c.id])),
        }
        for c in clientes
    ]
    return {
        "mes": _local(inicio, "America/Mexico_City").strftime("%Y-%m"),
        "venta_mes": str(_dinero(sum(venta.values(), CERO))),
        "comision_mes": str(_dinero(sum(comision.values(), CERO))),
        "con_venta": sum(1 for c in clientes if venta[c.id] > 0),
        "organizadores": filas,
    }


# --------------------------------------------------------------------------
# Cuentas (bloque 15)
# --------------------------------------------------------------------------


async def cuentas(
    db: AsyncSession, client_id: int | None, rol: str | None = None
) -> list[dict[str, Any]]:
    """Por cuenta: eventos asignados y su ultima funcion escaneada (cuantos
    dejo pasar y cuantos rechazo). Solo cuenta lo que escaneo esa persona."""
    q = select(User.id)
    if client_id is not None:
        q = q.where(User.client_id == client_id)
    if rol is not None:
        q = q.where(User.rol == rol)
    ids = [i for (i,) in (await db.execute(q)).all()]
    if not ids:
        return []
    asignados = dict(
        (
            await db.execute(
                select(AsignacionDeAcceso.user_id, func.count(AsignacionDeAcceso.id))
                .where(AsignacionDeAcceso.user_id.in_(ids))
                .group_by(AsignacionDeAcceso.user_id)
            )
        ).all()
    )
    # La ultima funcion de cada quien: la de su lectura mas reciente.
    ultima = (
        select(
            Scan.user_id,
            Scan.performance_id,
            func.row_number()
            .over(partition_by=Scan.user_id, order_by=Scan.escaneado_en.desc())
            .label("n"),
        )
        .where(Scan.user_id.in_(ids))
        .subquery()
    )
    funciones = dict(
        (
            await db.execute(
                select(ultima.c.user_id, ultima.c.performance_id).where(ultima.c.n == 1)
            )
        ).all()
    )
    lecturas: dict[tuple[int, int], tuple[int, int]] = {}
    datos_funcion: dict[int, tuple[Performance, Event, Venue]] = {}
    if funciones:
        for uid, pid, pasaron, rechazos in (
            await db.execute(
                select(
                    Scan.user_id,
                    Scan.performance_id,
                    func.count(Scan.id).filter(Scan.resultado == VALIDO),
                    func.count(Scan.id).filter(_rechazo()),
                )
                .where(
                    or_(
                        *[
                            (Scan.user_id == u) & (Scan.performance_id == p)
                            for u, p in funciones.items()
                        ]
                    )
                )
                .group_by(Scan.user_id, Scan.performance_id)
            )
        ).all():
            lecturas[(uid, pid)] = (int(pasaron), int(rechazos))
        for f, e, v in (
            await db.execute(
                select(Performance, Event, Venue)
                .join(Event, Event.id == Performance.event_id)
                .join(Venue, Venue.id == Event.venue_id)
                .where(Performance.id.in_(set(funciones.values())))
            )
        ).all():
            datos_funcion[f.id] = (f, e, v)
    salida = []
    for uid in ids:
        pid = funciones.get(uid)
        ult = None
        if pid is not None and pid in datos_funcion:
            f, e, v = datos_funcion[pid]
            pasaron, rechazos = lecturas.get((uid, pid), (0, 0))
            ult = {
                "performance_id": pid,
                "evento": e.nombre,
                "inicia_en": _local(f.inicia_en, v.zona_horaria).isoformat(),
                "escaneos": pasaron,
                "rechazos": rechazos,
            }
        salida.append(
            {
                "user_id": uid,
                "eventos_asignados": int(asignados.get(uid, 0)),
                "ultima_funcion": ult,
            }
        )
    return salida
