"""El QR del boleto y la vista "Tus boletos" (ADR 0014, BOL3).

El QR cambia cada 30 segundos (`PAL2`): una captura de pantalla deja de
servir en cuanto pasa su minuto, y la puerta solo acepta este QR.

    PAL2.<uuid base64url, 22>.<paso base36>.<firma base64url, 16>

- `paso` es el bloque de 30 s (segundos Unix // 30) para el que se genero.
- `firma` = HMAC(llave del boleto, version + uuid + paso).
- La llave del boleto = HMAC(`TICKET_SIGNING_KEY`, uuid + `semilla`). La
  recibe solo quien tiene la liga de la compra, para generar el QR en su
  celular aunque no tenga internet. Sin `TICKET_SIGNING_KEY` no se saca
  la llave de un boleto ni teniendo la base de datos.

La puerta acepta el paso actual, el anterior y el siguiente (relojes de
celular un poco adelantados o atrasados). El celular corrige su reloj con
la hora del servidor cada vez que abre sus boletos con internet.

`PAL1` era el QR fijo (el mismo siempre) que iba en el correo. Ya no deja
entrar: una captura o una foto del correo servian para siempre. Se sigue
leyendo solo para decirle a la puerta que es un QR viejo.
"""

import base64
import binascii
import hashlib
import hmac
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import ErrorDeDominio
from app.models.base import EstadoBoleto, EstadoFuncion, EstadoOrden
from app.models.catalog import Event, Performance, Venue
from app.models.sales import Customer, Order, OrderItem, Scan, Ticket
from app.services.checkout import consultar_orden

VERSION = "PAL1"
VERSION_ROTATORIA = "PAL2"
# Cada cuanto cambia el QR, en segundos.
PASO = 30
# Pasos de mas o de menos que acepta la puerta.
TOLERANCIA = 1
_BYTES_DE_FIRMA_ROTATORIA = 12
_DOMINIO_LLAVE = b"palco:boleto:qr2:llave:"
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


# ------------------------------------------------------------ QR rotatorio


def llave_rotatoria(
    boleto_uuid: UUID, semilla: bytes, llave: str | None = None
) -> bytes:
    """La llave con que el celular genera el QR de este boleto."""
    llave = llave or settings.TICKET_SIGNING_KEY
    return hmac.new(
        llave.encode("utf-8"),
        _DOMINIO_LLAVE + boleto_uuid.bytes + semilla,
        hashlib.sha256,
    ).digest()


def paso_de(momento: float) -> int:
    return int(momento // PASO)


def _firma_rotatoria(llave_boleto: bytes, boleto_uuid: UUID, paso: int) -> bytes:
    mensaje = (
        VERSION_ROTATORIA.encode("ascii") + boleto_uuid.bytes + paso.to_bytes(8, "big")
    )
    return hmac.new(llave_boleto, mensaje, hashlib.sha256).digest()[
        :_BYTES_DE_FIRMA_ROTATORIA
    ]


def _base36(n: int) -> str:
    digitos = "0123456789abcdefghijklmnopqrstuvwxyz"
    texto = ""
    while True:
        n, r = divmod(n, 36)
        texto = digitos[r] + texto
        if n == 0:
            return texto


def qr_rotatorio(boleto_uuid: UUID, llave_boleto: bytes, paso: int) -> str:
    """El QR de un paso. Lo genera el celular; aqui sirve para las pruebas."""
    firma = _firma_rotatoria(llave_boleto, boleto_uuid, paso)
    return (
        f"{VERSION_ROTATORIA}.{_b64(boleto_uuid.bytes)}.{_base36(paso)}.{_b64(firma)}"
    )


@dataclass(frozen=True)
class QRLeido:
    boleto_uuid: UUID
    # Solo PAL2. Un PAL1 bien firmado se reconoce para avisar que es viejo.
    paso: int | None = None
    firma: bytes | None = None

    @property
    def es_fijo(self) -> bool:
        return self.paso is None


def leer_cualquier_qr(texto: str, llave: str | None = None) -> QRLeido:
    """Lo que dice un QR de Palco, sin decidir aun si deja entrar.

    PAL1 se verifica completo (su firma no depende del boleto). PAL2 solo
    se desarma: su firma se revisa con la semilla del boleto
    (`verificar_rotatorio`).
    """
    limpio = texto.strip()
    if limpio.startswith(VERSION + "."):
        return QRLeido(leer_qr(limpio, llave))
    partes = limpio.split(".")
    if len(partes) != 4 or partes[0] != VERSION_ROTATORIA:
        raise QRInvalido("No es un boleto de Palco.")
    try:
        boleto_uuid = UUID(bytes=_de_b64(partes[1]))
        paso = int(partes[2], 36)
        firma = _de_b64(partes[3])
    except (ValueError, binascii.Error) as exc:
        raise QRInvalido("No es un boleto de Palco.") from exc
    if len(partes[2]) > 12 or len(firma) != _BYTES_DE_FIRMA_ROTATORIA:
        raise QRInvalido("No es un boleto de Palco.")
    return QRLeido(boleto_uuid, paso, firma)


def verificar_rotatorio(
    leido: QRLeido, semilla: bytes, ahora: float, llave: str | None = None
) -> str | None:
    """None si el QR deja entrar; si no, por que: "falso" o "vencido"."""
    assert leido.paso is not None and leido.firma is not None
    llave_boleto = llave_rotatoria(leido.boleto_uuid, semilla, llave)
    esperada = _firma_rotatoria(llave_boleto, leido.boleto_uuid, leido.paso)
    if not hmac.compare_digest(leido.firma, esperada):
        return "falso"
    if abs(leido.paso - paso_de(ahora)) > TOLERANCIA:
        return "vencido"
    return None


# ------------------------------------------------------- "Tus boletos"


@dataclass(frozen=True)
class BoletoVisible:
    uuid: UUID
    zona: str
    asiento: str
    estado: str
    # Solo los boletos que pueden entrar llevan llave: uno cancelado o usado
    # no debe verse como algo que todavia sirve. El codigo impreso no se
    # manda: dictado o en una captura dejaria entrar sin el QR.
    llave_qr: str | None
    # BOL8: cuando entro, si ya entro.
    entro_en: datetime | None
    # BOL6: a quien se lo paso quien compro ("Ana P."), mientras lo tenga.
    pasado_a: str | None = None
    # BOL6: lo que quien compro puede hacer con este boleto ahora.
    se_puede_pasar: bool = False
    se_puede_recuperar: bool = False


@dataclass(frozen=True)
class BoletosDeLaOrden:
    orden: Order
    funcion: Performance
    evento: Event
    recinto: Venue
    boletos: list[BoletoVisible]
    servidor_ahora: datetime


def nombre_corto(persona: Customer) -> str:
    """'Ana Pérez López' -> 'Ana P.'; sin nombre, el inicio del correo."""
    nombre = (persona.nombre or "").split()
    apellidos = (persona.apellidos or "").split()
    if nombre:
        return f"{nombre[0]} {apellidos[0][0]}." if apellidos else nombre[0]
    return f"{persona.email.split('@', 1)[0][:3]}…"


async def boletos_de_la_orden(db: AsyncSession, orden_uuid: UUID) -> BoletosDeLaOrden:
    """Lo que ve el comprador en "Tus boletos".

    El uuid de la orden es la llave de esta vista: no se adivina y solo
    lo tiene quien compro (pantalla de confirmacion y correo). Una orden
    que aun no se paga regresa la lista vacia; el front explica por que
    con el `estado`.

    Un boleto que paso a un acompanante (BOL6) sigue en la lista, sin
    llave: dice a quien se lo paso y si puede recuperarlo.
    """
    orden, _ = await consultar_orden(db, orden_uuid)
    return await vista_de_boletos(db, orden, orden.customer_id, comprador=True)


async def vista_de_boletos(
    db: AsyncSession, orden: Order, portador_id: int, comprador: bool
) -> BoletosDeLaOrden:
    """Los boletos de la orden como los ve `portador_id`.

    Quien compro ve todos; solo los que siguen siendo suyos llevan llave.
    Un acompanante (comprador=False) ve solo los que le pasaron.
    """
    # Aqui y no arriba: escaner ya importa este modulo.
    from app.services.escaner import horario_de_escaneo

    funcion = await db.get(Performance, orden.performance_id)
    evento = await db.get(Event, funcion.event_id)
    recinto = await db.get(Venue, evento.venue_id)

    consulta = (
        select(Ticket, OrderItem)
        .join(OrderItem, OrderItem.id == Ticket.order_item_id)
        .where(OrderItem.order_id == orden.id)
        .order_by(OrderItem.id)
        .execution_options(populate_existing=True)
    )
    if not comprador:
        consulta = consulta.where(Ticket.holder_customer_id == portador_id)
    filas = (await db.execute(consulta)).all()

    entradas = dict(
        (
            await db.execute(
                select(Scan.ticket_id, func.max(Scan.escaneado_en))
                .where(
                    Scan.ticket_id.in_([b.id for b, _ in filas]),
                    Scan.resultado == "valido",
                )
                .group_by(Scan.ticket_id)
            )
        ).all()
    )
    # De quien es cada boleto que ya no es de quien compro.
    otros = {b.holder_customer_id for b, _ in filas} - {portador_id}
    portadores = (
        {
            c.id: c
            for c in (
                await db.scalars(select(Customer).where(Customer.id.in_(otros)))
            ).all()
        }
        if comprador and otros
        else {}
    )

    ahora: datetime = await db.scalar(select(func.now()))
    pagada = orden.estado == EstadoOrden.PAGADA.value
    # Se pasa (y se recupera) hasta que abre el escaneo: despues, las puertas
    # sin internet ya tienen la lista con el QR de quien lo tenia.
    se_mueve = (
        comprador
        and pagada
        and funcion.estado != EstadoFuncion.CANCELADA.value
        and ahora < horario_de_escaneo(funcion)[0]
    )
    boletos = []
    for boleto, partida in filas:
        suyo = boleto.holder_customer_id == portador_id
        valido = boleto.estado == EstadoBoleto.VALIDO.value
        boletos.append(
            BoletoVisible(
                uuid=boleto.uuid,
                zona=partida.zona_snap,
                asiento=partida.asiento_snap,
                estado=boleto.estado,
                llave_qr=(
                    _b64(llave_rotatoria(boleto.uuid, boleto.semilla))
                    if pagada and valido and suyo
                    else None
                ),
                entro_en=(
                    entradas.get(boleto.id)
                    if boleto.estado == EstadoBoleto.USADO.value
                    else None
                ),
                pasado_a=(
                    nombre_corto(portadores[boleto.holder_customer_id])
                    if not suyo and boleto.holder_customer_id in portadores
                    else None
                ),
                se_puede_pasar=se_mueve and valido and suyo,
                se_puede_recuperar=se_mueve and valido and not suyo,
            )
        )
    return BoletosDeLaOrden(orden, funcion, evento, recinto, boletos, ahora)
