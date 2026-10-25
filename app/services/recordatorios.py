"""Correo recordatorio 24 h antes de la funcion (BOL10; decidido el 25 oct).

Le llega a quien compro y, aparte, a cada acompanante con su propia liga
(BOL6). Cada uno ve solo sus boletos y lleva "Ver mis boletos" y "Como
llegar".

Reglas:

- Sale cuando faltan 24 h o menos y la funcion no ha empezado.
- No sale si la compra (o el pase) se hizo con menos de 24 h de
  anticipacion: ya tiene su correo fresco.
- Una sola vez por compra y por pase: se programa en la bandeja de salida
  (`programar`) y la bandeja lo manda con reintentos, como los demas.
- Al mandarlo se revisa otra vez: si ya no tiene boletos que puedan entrar
  (reembolso, revocada, se lo recuperaron, ya entro) o la funcion se
  cancelo, no sale.
"""

from __future__ import annotations

from datetime import timedelta
from urllib.parse import quote_plus

from sqlalchemy import String, and_, cast, exists, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.base import (
    EstadoBoleto,
    EstadoFuncion,
    EstadoOrden,
    EstadoTransferencia,
)
from app.models.catalog import Performance, Venue
from app.models.outbox import OutboxEvent
from app.models.sales import Customer, Order, OrderItem, Ticket, Transfer
from app.services.boletos import (
    BoletosDeLaOrden,
    boletos_de_la_orden,
    vista_de_boletos,
)
from app.services.confirmacion import _fila_y_lugar, datos_del_correo, enlace_a_boletos
from app.services.correo import Mensaje
from app.services.correo_pases import enlace_del_pase
from app.services.plantillas import pintar_archivo

ANTES = timedelta(hours=24)
DEL_COMPRADOR = "funcion.recordatorio"
DEL_ACOMPANANTE = "pase.recordatorio"
_CANDADO = 7_410_010


def como_llegar(recinto: Venue) -> str:
    """Google Maps: por coordenadas si las hay; si no, por nombre y direccion."""
    if recinto.lat is not None and recinto.lng is not None:
        destino = f"{recinto.lat},{recinto.lng}"
    else:
        destino = ", ".join(
            p for p in (recinto.nombre, recinto.direccion, recinto.ciudad) if p
        )
    return f"https://www.google.com/maps/search/?api=1&query={quote_plus(destino)}"


async def programar(db: AsyncSession) -> int:
    """Deja en la bandeja los recordatorios que ya tocan. Cuantos se programaron.

    Con candado de la base: dos bandejas a la vez no los duplican. El que
    llama confirma la transaccion.
    """
    if not await db.scalar(text(f"SELECT pg_try_advisory_xact_lock({_CANDADO})")):
        return 0
    ahora = await db.scalar(select(func.now()))
    tocan = and_(
        Performance.inicia_en > ahora,
        Performance.inicia_en <= ahora + ANTES,
        Performance.estado != EstadoFuncion.CANCELADA.value,
    )

    compras = (
        await db.scalars(
            select(Order)
            .join(Performance, Performance.id == Order.performance_id)
            .where(
                tocan,
                Order.estado == EstadoOrden.PAGADA.value,
                Order.pagada_en <= Performance.inicia_en - ANTES,
                ~exists().where(
                    OutboxEvent.tipo == DEL_COMPRADOR,
                    OutboxEvent.agregado_uuid == Order.uuid,
                ),
            )
        )
    ).all()
    for orden in compras:
        db.add(
            OutboxEvent(
                tipo=DEL_COMPRADOR,
                agregado_uuid=orden.uuid,
                payload={"orden_uuid": str(orden.uuid)},
            )
        )

    # Uno por acompanante y por compra: si le pasaron 2 boletos, un solo
    # correo con sus 2 lugares (su liga muestra todos los que tiene).
    filas = (
        await db.execute(
            select(Transfer.id, Transfer.a_customer_id, Order.uuid)
            .join(Ticket, Ticket.id == Transfer.ticket_id)
            .join(OrderItem, OrderItem.id == Ticket.order_item_id)
            .join(Order, Order.id == OrderItem.order_id)
            .join(Performance, Performance.id == Order.performance_id)
            .where(
                tocan,
                Order.estado == EstadoOrden.PAGADA.value,
                Transfer.estado == EstadoTransferencia.ACEPTADA.value,
                Transfer.aceptada_en <= Performance.inicia_en - ANTES,
                Ticket.holder_customer_id == Transfer.a_customer_id,
                Ticket.estado == EstadoBoleto.VALIDO.value,
                ~exists().where(
                    OutboxEvent.tipo == DEL_ACOMPANANTE,
                    OutboxEvent.agregado_uuid == Order.uuid,
                    OutboxEvent.payload["acompanante_id"].astext
                    == cast(Transfer.a_customer_id, String),
                ),
            )
            .order_by(Transfer.id)
        )
    ).all()
    pases: dict[tuple, int] = {}
    for transferencia_id, acompanante_id, orden_uuid in filas:
        pases[(orden_uuid, acompanante_id)] = transferencia_id
    for (orden_uuid, acompanante_id), transferencia_id in pases.items():
        db.add(
            OutboxEvent(
                tipo=DEL_ACOMPANANTE,
                agregado_uuid=orden_uuid,
                payload={
                    "transferencia_id": transferencia_id,
                    "acompanante_id": acompanante_id,
                },
            )
        )
    await db.flush()
    return len(compras) + len(pases)


def _mensaje(
    vista: BoletosDeLaOrden,
    comprador: Customer,
    para: str,
    liga: str,
    boton: str,
) -> Mensaje | None:
    suyos = [b for b in vista.boletos if b.llave_qr]
    if not suyos:
        return None
    datos = datos_del_correo(vista, comprador, [], cortesia=True)
    nombre = vista.evento.nombre
    cuando = f"{datos['funcion']['fecha']} · {datos['funcion']['hora']}"
    asunto = f"Ya casi: {nombre}, {cuando}"
    lugares = []
    for b in suyos:
        fila, lugar = _fila_y_lugar(b.asiento)
        lugares.append(
            {
                "tipo": "Boleto general",
                "zona": b.zona,
                "fila": fila or "—",
                "lugar": lugar,
                "accesible": False,
            }
        )
    datos.update(
        {
            "asunto": asunto,
            "preheader": (
                f"{nombre}, {cuando}. Para entrar, abre tus boletos en tu celular."
            ),
            "titulo": "¡Ya casi es tu función!",
            "subtitulo": cuando,
            "recuperado": False,
            "boton": boton,
            "boletos_url": liga,
            "num_boletos": len(lugares),
            "boletos": lugares,
            "mapa_url": como_llegar(vista.recinto),
        }
    )
    return Mensaje(
        para=para,
        asunto=asunto,
        texto=pintar_archivo("correo/pase.txt.hbs", datos, escapar=False),
        html=pintar_archivo("correo/pase.hbs.html", datos),
    )


def _a_tiempo(vista: BoletosDeLaOrden) -> bool:
    return (
        vista.orden.estado == EstadoOrden.PAGADA.value
        and vista.funcion.estado != EstadoFuncion.CANCELADA.value
        and vista.servidor_ahora < vista.funcion.inicia_en
    )


async def correo_del_comprador(db: AsyncSession, orden_uuid) -> Mensaje | None:
    vista = await boletos_de_la_orden(db, orden_uuid)
    if not _a_tiempo(vista):
        return None
    comprador = await db.get(Customer, vista.orden.customer_id)
    para = vista.orden.email_snap or comprador.email
    return _mensaje(
        vista, comprador, para, enlace_a_boletos(vista.orden.uuid), "Ver mis boletos"
    )


async def correo_del_acompanante(db: AsyncSession, transferencia_id: int):
    pase = await db.get(Transfer, transferencia_id)
    if pase is None or pase.estado != EstadoTransferencia.ACEPTADA.value:
        return None
    boleto = await db.get(Ticket, pase.ticket_id)
    if (
        boleto.holder_customer_id != pase.a_customer_id
        or boleto.estado != EstadoBoleto.VALIDO.value
    ):
        return None
    partida = await db.get(OrderItem, boleto.order_item_id)
    orden = await db.get(Order, partida.order_id)
    vista = await vista_de_boletos(db, orden, pase.a_customer_id, comprador=False)
    if not _a_tiempo(vista):
        return None
    acompanante = await db.get(Customer, pase.a_customer_id)
    return _mensaje(
        vista,
        acompanante,
        pase.para_email,
        enlace_del_pase(pase.token),
        "Ver mi boleto" if len(vista.boletos) == 1 else "Ver mis boletos",
    )
