"""Listado de ventas de una funcion (RP1).

Para PSD y el organizador dueno: cada butaca vendida o regalada, con
quien la compro, cuanto pago, por donde y si ya entro. Lleva correo y
telefono del comprador, asi que cada consulta queda en la bitacora
(`data_access_log`): quien, cuando, y si la vio o la descargo. El
personal de acceso no lo ve nunca: lo suyo es la lista de la puerta.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from openpyxl import Workbook
from openpyxl.cell import WriteOnlyCell
from openpyxl.styles import Font
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.acceso import ConsultaDeDatos
from app.models.base import EstadoBoleto, EstadoOrden
from app.models.catalog import Event, Performance, Venue
from app.models.sales import Cortesia, Customer, Order, OrderItem, Scan, Ticket
from app.services.escaner import VALIDO

RECURSO = "ventas_funcion"
RECURSO_EVENTO = "ventas_evento"
# Lo que fue venta en algun momento. Pendientes y expiradas nunca se cobraron.
VENDIDAS = (
    EstadoOrden.PAGADA.value,
    EstadoOrden.CANCELADA.value,
    EstadoOrden.REEMBOLSADA.value,
)


@dataclass(frozen=True)
class RenglonDeVenta:
    folio: str
    comprada_en: datetime
    zona: str
    asiento: str
    titular: str
    precio: Decimal
    descuento: Decimal
    iva: Decimal
    total: Decimal
    canal: str
    motivo_cortesia: str | None
    estado: str
    entro_en: datetime | None
    email: str
    telefono: str | None
    # El codigo impreso del boleto: con el se deshace una entrada (E3d).
    codigo: str | None = None


@dataclass(frozen=True)
class ResumenDeVentas:
    boletos: int
    cortesias: int
    importe: Decimal
    iva: Decimal
    canceladas: int


@dataclass(frozen=True)
class ListadoDeVentas:
    evento: str
    recinto: str
    zona_horaria: str
    inicia_en: datetime
    resumen: ResumenDeVentas
    ventas: tuple[RenglonDeVenta, ...]


def _canal(orden: Order) -> str:
    if orden.canal == "cortesia":
        return "cortesia"
    return orden.canal or "web"


def _estado(orden: Order, partida: OrderItem) -> str:
    if orden.cerrada_motivo == "cortesia_revocada":
        return "revocada"
    if orden.estado == EstadoOrden.PAGADA.value and partida.anulado:
        return "cancelada"
    return orden.estado


class VentasService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def registrar(
        self,
        usuario_id: int,
        performance_id: int | None,
        accion: str,
        event_id: int | None = None,
    ) -> None:
        self.db.add(
            ConsultaDeDatos(
                user_id=usuario_id,
                recurso=RECURSO if event_id is None else RECURSO_EVENTO,
                performance_id=performance_id,
                event_id=event_id,
                accion=accion,
            )
        )
        await self.db.flush()

    async def de_evento(
        self, evento: Event
    ) -> list[tuple[Performance, ListadoDeVentas]]:
        """Cada funcion del evento con su listado, en orden de fecha (RP2)."""
        funciones = (
            await self.db.scalars(
                select(Performance)
                .where(Performance.event_id == evento.id)
                .order_by(Performance.inicia_en)
            )
        ).all()
        return [(f, await self.de_funcion(f, evento)) for f in funciones]

    async def de_funcion(self, funcion: Performance, evento: Event) -> ListadoDeVentas:
        recinto = await self.db.get(Venue, evento.venue_id)
        # La entrada vigente: la ultima lectura que lo dejo pasar. Si una
        # entrada se deshizo, el boleto vuelve a valido y no cuenta (E3d).
        entrada = (
            select(Scan.ticket_id, func.max(Scan.escaneado_en).label("entro_en"))
            .where(Scan.performance_id == funcion.id, Scan.resultado == VALIDO)
            .group_by(Scan.ticket_id)
            .subquery()
        )
        filas = (
            await self.db.execute(
                select(
                    OrderItem,
                    Order,
                    Customer,
                    Cortesia.motivo,
                    entrada.c.entro_en,
                    Ticket.codigo,
                    Ticket.estado,
                )
                .join(Order, Order.id == OrderItem.order_id)
                .join(Customer, Customer.id == Order.customer_id)
                .outerjoin(Cortesia, Cortesia.order_id == Order.id)
                .outerjoin(Ticket, Ticket.order_item_id == OrderItem.id)
                .outerjoin(entrada, entrada.c.ticket_id == Ticket.id)
                .where(
                    Order.performance_id == funcion.id,
                    Order.estado.in_(VENDIDAS),
                    Order.pagada_en.is_not(None),
                )
                .order_by(Order.pagada_en, OrderItem.id)
            )
        ).all()

        ventas = []
        for partida, orden, comprador, motivo, entro_en, codigo, estado in filas:
            nombre = " ".join(
                p
                for p in (
                    orden.nombre_snap or comprador.nombre,
                    orden.apellidos_snap if orden.nombre_snap else comprador.apellidos,
                )
                if p
            )
            ventas.append(
                RenglonDeVenta(
                    folio=orden.folio,
                    comprada_en=orden.pagada_en,
                    zona=partida.zona_snap,
                    asiento=partida.asiento_snap,
                    titular=nombre,
                    precio=partida.precio_unitario,
                    descuento=partida.descuento,
                    iva=partida.iva,
                    total=partida.importe,
                    canal=_canal(orden),
                    motivo_cortesia=motivo,
                    estado=_estado(orden, partida),
                    entro_en=entro_en if estado == EstadoBoleto.USADO.value else None,
                    email=comprador.email,
                    telefono=comprador.telefono,
                    codigo=codigo,
                )
            )

        vigentes = [v for v in ventas if v.estado == EstadoOrden.PAGADA.value]
        resumen = ResumenDeVentas(
            boletos=sum(1 for v in vigentes if v.canal != "cortesia"),
            cortesias=sum(1 for v in vigentes if v.canal == "cortesia"),
            importe=sum((v.total for v in vigentes), Decimal("0")),
            iva=sum((v.iva for v in vigentes), Decimal("0")),
            canceladas=len(ventas) - len(vigentes),
        )
        return ListadoDeVentas(
            evento=evento.nombre,
            recinto=recinto.nombre if recinto else "",
            zona_horaria=recinto.zona_horaria if recinto else "America/Mexico_City",
            inicia_en=funcion.inicia_en,
            resumen=resumen,
            ventas=tuple(ventas),
        )


COLUMNAS = (
    ("Folio", 16),
    ("Fecha de compra", 18),
    ("Zona", 16),
    ("Asiento", 16),
    ("A nombre de", 28),
    ("Precio", 11),
    ("Descuento", 11),
    ("IVA", 10),
    ("Total", 11),
    ("Canal", 10),
    ("Motivo de cortesía", 22),
    ("Estado", 12),
    ("Entró", 18),
    ("Correo", 30),
    ("Teléfono", 16),
    ("Código", 14),
)
_FECHAS = ("Fecha de compra", "Entró", "Función")
_DINERO = ("Precio", "Descuento", "IVA", "Total")


def _texto(hoja, valor: str | None) -> WriteOnlyCell:
    # Siempre texto: un nombre o correo que empiece con "=" no se vuelve
    # formula al abrir el archivo, y "+52..." no se vuelve numero.
    celda = WriteOnlyCell(hoja, value=valor or "")
    celda.data_type = "s"
    return celda


def _libro(partes: list[tuple[datetime | None, ListadoDeVentas]]) -> bytes:
    """Un .xlsx con las horas en la zona del recinto.

    Con la fecha de la funcion en cada parte (todo un evento), la primera
    columna dice de que funcion es cada lugar.
    """
    con_funcion = any(inicia for inicia, _ in partes)
    columnas = ((("Función", 18),) if con_funcion else ()) + COLUMNAS
    libro = Workbook(write_only=True)
    hoja = libro.create_sheet("Ventas")
    for i, (_, ancho) in enumerate(columnas):
        hoja.column_dimensions[chr(ord("A") + i)].width = ancho
    hoja.freeze_panes = "A2"

    titulo = []
    for nombre, _ in columnas:
        celda = _texto(hoja, nombre)
        celda.font = Font(bold=True)
        titulo.append(celda)
    hoja.append(titulo)

    for inicia, listado in partes:
        zona = ZoneInfo(listado.zona_horaria)

        def local(momento: datetime | None, zona=zona) -> datetime | None:
            return momento.astimezone(zona).replace(tzinfo=None) if momento else None

        for v in listado.ventas:
            valores = {
                "Función": WriteOnlyCell(hoja, value=local(inicia)),
                "Folio": _texto(hoja, v.folio),
                "Fecha de compra": WriteOnlyCell(hoja, value=local(v.comprada_en)),
                "Zona": _texto(hoja, v.zona),
                "Asiento": _texto(hoja, v.asiento),
                "A nombre de": _texto(hoja, v.titular),
                "Precio": WriteOnlyCell(hoja, value=v.precio),
                "Descuento": WriteOnlyCell(hoja, value=v.descuento),
                "IVA": WriteOnlyCell(hoja, value=v.iva),
                "Total": WriteOnlyCell(hoja, value=v.total),
                "Canal": _texto(hoja, v.canal),
                "Motivo de cortesía": _texto(hoja, v.motivo_cortesia),
                "Estado": _texto(hoja, v.estado),
                "Entró": WriteOnlyCell(hoja, value=local(v.entro_en)),
                "Correo": _texto(hoja, v.email),
                "Teléfono": _texto(hoja, v.telefono),
                "Código": _texto(hoja, v.codigo),
            }
            fila = []
            for nombre, _ in columnas:
                celda = valores[nombre]
                if nombre in _FECHAS:
                    celda.number_format = "dd/mm/yyyy hh:mm"
                elif nombre in _DINERO:
                    celda.number_format = '"$"#,##0.00'
                fila.append(celda)
            hoja.append(fila)

    salida = io.BytesIO()
    libro.save(salida)
    return salida.getvalue()


def a_excel(listado: ListadoDeVentas) -> bytes:
    """El listado de una funcion como .xlsx."""
    return _libro([(None, listado)])


def a_excel_evento(partes: list[tuple[Performance, ListadoDeVentas]]) -> bytes:
    """Todas las funciones de un evento en un solo .xlsx, con su columna Función."""
    return _libro([(f.inicia_en, listado) for f, listado in partes])
