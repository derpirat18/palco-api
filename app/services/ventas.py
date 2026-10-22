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
from app.models.base import EstadoOrden
from app.models.catalog import Event, Performance, Venue
from app.models.sales import Cortesia, Customer, Order, OrderItem, Scan, Ticket
from app.services.escaner import VALIDO

RECURSO = "ventas_funcion"
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
        self, usuario_id: int, performance_id: int, accion: str
    ) -> None:
        self.db.add(
            ConsultaDeDatos(
                user_id=usuario_id,
                recurso=RECURSO,
                performance_id=performance_id,
                accion=accion,
            )
        )
        await self.db.flush()

    async def de_funcion(self, funcion: Performance, evento: Event) -> ListadoDeVentas:
        recinto = await self.db.get(Venue, evento.venue_id)
        primera_entrada = (
            select(Scan.ticket_id, func.min(Scan.escaneado_en).label("entro_en"))
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
                    primera_entrada.c.entro_en,
                )
                .join(Order, Order.id == OrderItem.order_id)
                .join(Customer, Customer.id == Order.customer_id)
                .outerjoin(Cortesia, Cortesia.order_id == Order.id)
                .outerjoin(Ticket, Ticket.order_item_id == OrderItem.id)
                .outerjoin(primera_entrada, primera_entrada.c.ticket_id == Ticket.id)
                .where(
                    Order.performance_id == funcion.id,
                    Order.estado.in_(VENDIDAS),
                    Order.pagada_en.is_not(None),
                )
                .order_by(Order.pagada_en, OrderItem.id)
            )
        ).all()

        ventas = []
        for partida, orden, comprador, motivo, entro_en in filas:
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
                    entro_en=entro_en,
                    email=comprador.email,
                    telefono=comprador.telefono,
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
)


def _texto(hoja, valor: str | None) -> WriteOnlyCell:
    # Siempre texto: un nombre o correo que empiece con "=" no se vuelve
    # formula al abrir el archivo, y "+52..." no se vuelve numero.
    celda = WriteOnlyCell(hoja, value=valor or "")
    celda.data_type = "s"
    return celda


def a_excel(listado: ListadoDeVentas) -> bytes:
    """El listado como .xlsx, con las horas en la zona del recinto."""
    zona = ZoneInfo(listado.zona_horaria)

    def local(momento: datetime | None) -> datetime | None:
        return momento.astimezone(zona).replace(tzinfo=None) if momento else None

    libro = Workbook(write_only=True)
    hoja = libro.create_sheet("Ventas")
    for i, (_, ancho) in enumerate(COLUMNAS):
        hoja.column_dimensions[chr(ord("A") + i)].width = ancho
    hoja.freeze_panes = "A2"

    titulo = []
    for nombre, _ in COLUMNAS:
        celda = _texto(hoja, nombre)
        celda.font = Font(bold=True)
        titulo.append(celda)
    hoja.append(titulo)

    for v in listado.ventas:
        fila = [
            _texto(hoja, v.folio),
            WriteOnlyCell(hoja, value=local(v.comprada_en)),
            _texto(hoja, v.zona),
            _texto(hoja, v.asiento),
            _texto(hoja, v.titular),
            WriteOnlyCell(hoja, value=v.precio),
            WriteOnlyCell(hoja, value=v.descuento),
            WriteOnlyCell(hoja, value=v.iva),
            WriteOnlyCell(hoja, value=v.total),
            _texto(hoja, v.canal),
            _texto(hoja, v.motivo_cortesia),
            _texto(hoja, v.estado),
            WriteOnlyCell(hoja, value=local(v.entro_en)),
            _texto(hoja, v.email),
            _texto(hoja, v.telefono),
        ]
        for i in (1, 12):
            fila[i].number_format = "dd/mm/yyyy hh:mm"
        for i in (5, 6, 7, 8):
            fila[i].number_format = '"$"#,##0.00'
        hoja.append(fila)

    salida = io.BytesIO()
    libro.save(salida)
    return salida.getvalue()
