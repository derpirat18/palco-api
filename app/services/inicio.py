"""La pantalla de Inicio del panel y el buscador de arriba.

Inicio: lo que esta pasando hoy (venta y boletos de hoy contra ayer,
funciones de la semana, cajas abiertas), las proximas funciones con lo
vendido, la venta de los ultimos 7 dias y lo que esta pendiente. PSD ve
todo; el organizador, lo suyo. Los dias son de la hora del centro.

Buscar: eventos por nombre, compras por folio y compras por correo del
comprador (el escrito en esa compra). Buscar un correo es consultar datos
personales: queda en la bitacora.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.acceso import ConsultaDeDatos
from app.models.base import EstadoAsiento, EstadoEvento, EstadoFuncion, EstadoOrden
from app.models.catalog import Event, Performance, Venue
from app.models.inventory import PerformanceSeat
from app.models.sales import Order, OrderItem
from app.models.taquilla import ABIERTA, CONTADA, CajaDeTaquilla, VentaDeTaquilla
from app.models.users import User
from app.services.reportes import ZONA, _dinero, _local

CERO = Decimal("0")


def _dia(d) -> tuple[datetime, datetime]:
    ini = datetime.combine(d, time.min, ZONA).astimezone(UTC)
    return ini, ini + timedelta(days=1)


def _del_cliente(q, client_id: int | None):
    return q if client_id is None else q.where(Event.client_id == client_id)


async def _venta(db: AsyncSession, client_id, ini, fin) -> tuple[Decimal, int]:
    q = (
        select(func.coalesce(func.sum(OrderItem.importe), 0), func.count(OrderItem.id))
        .join(Order, Order.id == OrderItem.order_id)
        .join(Performance, Performance.id == Order.performance_id)
        .join(Event, Event.id == Performance.event_id)
        .where(
            Order.estado == EstadoOrden.PAGADA.value,
            OrderItem.anulado.is_(False),
            or_(Order.canal.is_(None), Order.canal != "cortesia"),
            Order.pagada_en >= ini,
            Order.pagada_en < fin,
        )
    )
    monto, boletos = (await db.execute(_del_cliente(q, client_id))).one()
    return _dinero(monto), int(boletos)


async def inicio(db: AsyncSession, client_id: int | None) -> dict[str, Any]:
    hoy = datetime.now(ZONA).date()
    venta_hoy, boletos_hoy = await _venta(db, client_id, *_dia(hoy))
    venta_ayer, boletos_ayer = await _venta(
        db, client_id, *_dia(hoy - timedelta(days=1))
    )

    ahora = datetime.now(UTC)
    semana_ini, _ = _dia(hoy)
    semana_fin = semana_ini + timedelta(days=7)
    q = (
        select(func.count(Performance.id))
        .join(Event, Event.id == Performance.event_id)
        .where(
            Performance.inicia_en >= semana_ini,
            Performance.inicia_en < semana_fin,
            Performance.estado != EstadoFuncion.CANCELADA.value,
        )
    )
    funciones_semana = await db.scalar(_del_cliente(q, client_id))

    # Cajas abiertas y el efectivo que llevan.
    q = (
        select(CajaDeTaquilla.id)
        .join(Event, Event.id == CajaDeTaquilla.event_id)
        .where(CajaDeTaquilla.estado == ABIERTA)
    )
    abiertas = list((await db.scalars(_del_cliente(q, client_id))).all())
    efectivo = CERO
    if abiertas:
        efectivo = await db.scalar(
            select(func.coalesce(func.sum(VentaDeTaquilla.total), 0)).where(
                VentaDeTaquilla.caja_id.in_(abiertas),
                VentaDeTaquilla.anulada_en.is_(None),
                VentaDeTaquilla.forma_pago == "efectivo",
            )
        )
    q = (
        select(func.count(CajaDeTaquilla.id))
        .join(Event, Event.id == CajaDeTaquilla.event_id)
        .where(CajaDeTaquilla.estado == CONTADA)
    )
    por_cerrar = await db.scalar(_del_cliente(q, client_id))

    # Proximas funciones (las que empezaron hace menos de 4 h tambien).
    q = (
        select(Performance, Event, Venue)
        .join(Event, Event.id == Performance.event_id)
        .join(Venue, Venue.id == Event.venue_id)
        .where(
            Performance.inicia_en >= ahora - timedelta(hours=4),
            Performance.estado != EstadoFuncion.CANCELADA.value,
            Event.estado != EstadoEvento.ARCHIVADO.value,
        )
        .order_by(Performance.inicia_en)
        .limit(6)
    )
    proximas_filas = (await db.execute(_del_cliente(q, client_id))).all()
    conteos: dict[int, tuple[int, int, int]] = {}
    ids = [p.id for p, _, _ in proximas_filas]
    if ids:
        for pid, cap, ocupados, libres in (
            await db.execute(
                select(
                    PerformanceSeat.performance_id,
                    func.count(PerformanceSeat.id),
                    func.count(PerformanceSeat.id).filter(
                        PerformanceSeat.estado.in_(
                            (EstadoAsiento.VENDIDO.value, EstadoAsiento.CORTESIA.value)
                        )
                    ),
                    func.count(PerformanceSeat.id).filter(
                        PerformanceSeat.estado == EstadoAsiento.DISPONIBLE.value
                    ),
                )
                .where(
                    PerformanceSeat.performance_id.in_(ids),
                    PerformanceSeat.estado != EstadoAsiento.KILL.value,
                )
                .group_by(PerformanceSeat.performance_id)
            )
        ).all():
            conteos[pid] = (cap, ocupados, libres)
    proximas = []
    sin_venta = 0
    for p, e, v in proximas_filas:
        cap, ocupados, libres = conteos.get(p.id, (0, 0, 0))
        if e.estado == EstadoEvento.BORRADOR.value:
            estado = ("mute", "Borrador")
        elif not p.inventario_generado:
            estado = ("warn", "Sin lugares")
        elif p.estado == EstadoFuncion.EN_VENTA.value and cap and not libres:
            estado = ("bad", "Agotado")
        elif p.estado == EstadoFuncion.EN_VENTA.value:
            estado = ("good", "En venta")
        else:
            estado = ("mute", "Sin venta")
        if estado[1] in ("Borrador", "Sin lugares", "Sin venta"):
            sin_venta += 1
        proximas.append(
            {
                "performance_id": p.id,
                "event_id": e.id,
                "evento": e.nombre,
                "recinto": v.nombre,
                "ciudad": v.ciudad,
                "inicia_en": _local(p.inicia_en, v.zona_horaria).isoformat(),
                "capacidad": cap,
                "vendidos": ocupados,
                "estado": estado[1],
                "tono": estado[0],
            }
        )

    # Venta de los ultimos 7 dias.
    ini7, _ = _dia(hoy - timedelta(days=6))
    q = (
        select(Order.pagada_en, OrderItem.importe)
        .join(Order, Order.id == OrderItem.order_id)
        .join(Performance, Performance.id == Order.performance_id)
        .join(Event, Event.id == Performance.event_id)
        .where(
            Order.estado == EstadoOrden.PAGADA.value,
            OrderItem.anulado.is_(False),
            or_(Order.canal.is_(None), Order.canal != "cortesia"),
            Order.pagada_en >= ini7,
        )
    )
    por_dia: dict[str, Decimal] = defaultdict(lambda: CERO)
    for pagada, importe in (await db.execute(_del_cliente(q, client_id))).all():
        por_dia[_local(pagada).date().isoformat()] += importe
    dias = [(hoy - timedelta(days=i)).isoformat() for i in range(6, -1, -1)]

    pendientes = []
    if sin_venta:
        pendientes.append(
            {
                "tono": "warn",
                "titulo": (
                    f"{sin_venta} función próxima sin venta abierta"
                    if sin_venta == 1
                    else f"{sin_venta} funciones próximas sin venta abierta"
                ),
                "detalle": "Revisa precios, lugares y la venta en cada función.",
                "a": "/panel/eventos",
            }
        )
    if por_cerrar:
        pendientes.append(
            {
                "tono": "bad",
                "titulo": (
                    "1 caja contada sin cerrar"
                    if por_cerrar == 1
                    else f"{por_cerrar} cajas contadas sin cerrar"
                ),
                "detalle": "El jefe de taquilla revisa la diferencia y la cierra.",
                "a": "/panel/taquilla",
            }
        )
    return {
        "venta_hoy": str(venta_hoy),
        "venta_ayer": str(venta_ayer),
        "boletos_hoy": boletos_hoy,
        "boletos_ayer": boletos_ayer,
        "funciones_semana": int(funciones_semana or 0),
        "cajas_abiertas": len(abiertas),
        "efectivo_en_cajas": str(_dinero(efectivo)),
        "proximas": proximas,
        "venta_7_dias": [{"dia": d, "monto": str(_dinero(por_dia[d]))} for d in dias],
        "pendientes": pendientes,
    }


async def buscar(
    db: AsyncSession, usuario: User, client_id: int | None, q: str
) -> list[dict[str, Any]]:
    texto = q.strip()
    if len(texto) < 3:
        return []
    resultados: list[dict[str, Any]] = []
    eventos = (
        await db.execute(
            _del_cliente(
                select(Event.id, Event.nombre, Venue.ciudad)
                .join(Venue, Venue.id == Event.venue_id)
                .where(Event.nombre.ilike(f"%{_comodines(texto)}%"))
                .order_by(Event.nombre)
                .limit(6),
                client_id,
            )
        )
    ).all()
    resultados += [
        {"tipo": "evento", "titulo": n, "detalle": c, "a": f"/panel/eventos/{i}"}
        for i, n, c in eventos
    ]
    es_correo = "@" in texto
    columna = Order.email_snap if es_correo else Order.folio
    patron = texto.lower() if es_correo else texto.upper()
    compras = (
        await db.execute(
            _del_cliente(
                select(
                    Order.folio,
                    Order.email_snap,
                    Order.performance_id,
                    Event.nombre,
                    Performance.inicia_en,
                    Venue.zona_horaria,
                )
                .join(Performance, Performance.id == Order.performance_id)
                .join(Event, Event.id == Performance.event_id)
                .join(Venue, Venue.id == Event.venue_id)
                .where(
                    Order.estado.in_(
                        (
                            EstadoOrden.PAGADA.value,
                            EstadoOrden.CANCELADA.value,
                            EstadoOrden.REEMBOLSADA.value,
                        )
                    ),
                    columna == patron
                    if es_correo
                    else columna.ilike(f"%{_comodines(patron)}%"),
                )
                .order_by(Order.pagada_en.desc())
                .limit(8),
                client_id,
            )
        )
    ).all()
    for folio, correo, pid, evento, inicia, zh in compras:
        resultados.append(
            {
                "tipo": "compra",
                "titulo": folio,
                "detalle": f"{correo} · {evento} · {_local(inicia, zh):%d/%m %H:%M}",
                "a": f"/panel/funciones/{pid}/ventas?folio={folio}",
            }
        )
    if es_correo:
        db.add(
            ConsultaDeDatos(
                user_id=usuario.id,
                recurso="busqueda",
                accion="ver",
                filtros={"q": texto, "resultados": len(compras)},
            )
        )
        await db.flush()
    return resultados


def _comodines(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
