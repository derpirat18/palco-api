"""El QR del boleto y la vista "Tus boletos" (ADR 0014).

El QR lleva solo el uuid del boleto y una firma HMAC con
`TICKET_SIGNING_KEY`. Sin la llave nadie fabrica un QR que la puerta
acepte, y el QR no dice nada de quien lo compro: si se fotografia o se
comparte, lo unico que se filtra es un identificador opaco.

Formato, 50 caracteres para que el escaner lo lea a la primera:

    PAL1.<uuid en base64url, 22>.<firma en base64url, 22>

`PAL1` es la version. Cuando se encienda el QR rotatorio con la
`semilla` de cada boleto, sera otra version y los QR de `PAL1` ya
impresos seguiran leyendose.
"""

import base64
import binascii
import hashlib
import hmac
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import ErrorDeDominio
from app.models.base import EstadoBoleto, EstadoOrden
from app.models.catalog import Event, Performance, Venue
from app.models.sales import Order, OrderItem, Ticket
from app.services.checkout import consultar_orden

VERSION = "PAL1"
# 128 bits de firma: imposible de adivinar y cabe en un QR chico.
_BYTES_DE_FIRMA = 16
# Separa este uso de la llave de cualquier otro que se le de despues.
_DOMINIO = b"palco:boleto:qr:"


class QRInvalido(ErrorDeDominio):
    """El texto no es un QR de Palco o su firma no cuadra."""

    codigo = "qr_invalido"
    http_status = 422


def _b64(datos: bytes) -> str:
    return base64.urlsafe_b64encode(datos).rstrip(b"=").decode("ascii")


def _de_b64(texto: str) -> bytes:
    return base64.urlsafe_b64decode(texto + "=" * (-len(texto) % 4))


def _firma(boleto_uuid: UUID, llave: str) -> bytes:
    mensaje = _DOMINIO + VERSION.encode("ascii") + boleto_uuid.bytes
    return hmac.new(llave.encode("utf-8"), mensaje, hashlib.sha256).digest()[
        :_BYTES_DE_FIRMA
    ]


def contenido_del_qr(boleto_uuid: UUID, llave: str | None = None) -> str:
    llave = llave or settings.TICKET_SIGNING_KEY
    return f"{VERSION}.{_b64(boleto_uuid.bytes)}.{_b64(_firma(boleto_uuid, llave))}"


def leer_qr(texto: str, llave: str | None = None) -> UUID:
    """El uuid del boleto, solo si el QR lo firmo Palco."""
    llave = llave or settings.TICKET_SIGNING_KEY
    partes = texto.strip().split(".")
    if len(partes) != 3 or partes[0] != VERSION:
        raise QRInvalido("No es un boleto de Palco.")
    try:
        crudo = _de_b64(partes[1])
        firma = _de_b64(partes[2])
        boleto_uuid = UUID(bytes=crudo)
    except (ValueError, binascii.Error) as exc:
        raise QRInvalido("No es un boleto de Palco.") from exc
    # compare_digest: el tiempo de comparacion no delata cuantos bytes
    # acerto un falsificador.
    if not hmac.compare_digest(firma, _firma(boleto_uuid, llave)):
        raise QRInvalido("No es un boleto de Palco.")
    return boleto_uuid


@dataclass(frozen=True)
class BoletoVisible:
    uuid: UUID
    codigo: str
    zona: str
    asiento: str
    estado: str
    # Solo los boletos que pueden entrar llevan QR: uno cancelado o usado
    # no debe verse como algo que todavia sirve.
    qr: str | None


@dataclass(frozen=True)
class BoletosDeLaOrden:
    orden: Order
    funcion: Performance
    evento: Event
    recinto: Venue
    boletos: list[BoletoVisible]


async def boletos_de_la_orden(db: AsyncSession, orden_uuid: UUID) -> BoletosDeLaOrden:
    """Lo que ve el comprador en "Tus boletos".

    El uuid de la orden es la llave de esta vista: no se adivina y solo
    lo tiene quien compro (pantalla de confirmacion y correo). Una orden
    que aun no se paga regresa la lista vacia; el front explica por que
    con el `estado`.
    """
    orden, _ = await consultar_orden(db, orden_uuid)
    funcion = await db.get(Performance, orden.performance_id)
    evento = await db.get(Event, funcion.event_id)
    recinto = await db.get(Venue, evento.venue_id)

    filas = (
        await db.execute(
            select(Ticket, OrderItem)
            .join(OrderItem, OrderItem.id == Ticket.order_item_id)
            .where(OrderItem.order_id == orden.id)
            .order_by(OrderItem.id)
            .execution_options(populate_existing=True)
        )
    ).all()

    pagada = orden.estado == EstadoOrden.PAGADA.value
    boletos = [
        BoletoVisible(
            uuid=boleto.uuid,
            codigo=boleto.codigo,
            zona=partida.zona_snap,
            asiento=partida.asiento_snap,
            estado=boleto.estado,
            qr=(
                contenido_del_qr(boleto.uuid)
                if pagada and boleto.estado == EstadoBoleto.VALIDO.value
                else None
            ),
        )
        for boleto, partida in filas
    ]
    return BoletosDeLaOrden(orden, funcion, evento, recinto, boletos)
