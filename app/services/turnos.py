"""Puerta del turno: la API no deja empezar una compra sin turno (ADR 0017).

Es la mitad de servidor de la sala de espera (B17). La sala, que llega
despues, forma a los compradores y les entrega un **pase**; esta puerta
lo exige al apartar. Asi la fila no se brinca llamando a la API directo.

Esta apagada mientras `SALA_DE_ESPERA_HABILITADA` sea false: no consulta
nada y no cuesta nada. Encendida, solo exige pase cuando la sala de ese
evento esta activa (`sala:activa:{evento_uuid}` en Redis).

Reglas:

- Solo se exige pase para **empezar** un apartado. Quien ya tenia uno
  sigue sumando y quitando butacas sin pase, hasta su vencimiento normal.
- El pase es de un evento y vence. Firmado con `SECRET_KEY` y un prefijo
  de dominio propio, como el QR del boleto: sin la llave no se fabrica.
- Un pase abre a lo mas `APARTADOS_POR_PASE` apartados distintos. Con
  dos alcanza para quien solto el suyo y volvio a elegir; compartir el
  pase con un bot no le sirve de mucho.
- Si Redis no responde, se deja pasar: el apartado depende de Redis y
  fallara por su cuenta.

Formato del pase:

    T1.<evento uuid, 22>.<id del pase, 22>.<vence, epoch en base 36>.<firma, 22>
"""

import base64
import binascii
import hashlib
import hmac
import logging
import secrets
import time
from dataclasses import dataclass
from uuid import UUID

from redis.asyncio.client import Redis
from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import ErrorDeDominio
from app.models.catalog import Event, Performance
from app.models.inventory import PerformanceSeat

log = logging.getLogger("palco.sala")

VERSION = "T1"
ENCABEZADO = "X-Palco-Turno"
APARTADOS_POR_PASE = 2
_DOMINIO = b"palco:turno:"
_BYTES_DE_FIRMA = 16

MENSAJE_SALA = (
    "Hay muchas personas intentando comprar. Estamos dando acceso por turnos "
    "para mantener una compra estable. Estar en la sala de espera no garantiza "
    "boletos; la disponibilidad se confirma al seleccionar tus lugares."
)


class TurnoRequerido(ErrorDeDominio):
    codigo = "turno_requerido"
    http_status = 403


class TurnoVencido(ErrorDeDominio):
    codigo = "turno_vencido"
    http_status = 403


class TurnoUsado(ErrorDeDominio):
    codigo = "turno_usado"
    http_status = 403


class PaseInvalido(Exception):
    pass


@dataclass(frozen=True)
class Pase:
    evento_uuid: UUID
    pase_id: str
    vence: int


def _b64(datos: bytes) -> str:
    return base64.urlsafe_b64encode(datos).rstrip(b"=").decode("ascii")


def _de_b64(texto: str) -> bytes:
    return base64.urlsafe_b64decode(texto + "=" * (-len(texto) % 4))


def _base36(n: int) -> str:
    digitos = "0123456789abcdefghijklmnopqrstuvwxyz"
    texto = ""
    while n:
        n, r = divmod(n, 36)
        texto = digitos[r] + texto
    return texto or "0"


def _firma(cuerpo: str, llave: str) -> str:
    mensaje = _DOMINIO + cuerpo.encode("ascii")
    digest = hmac.new(llave.encode("utf-8"), mensaje, hashlib.sha256).digest()
    return _b64(digest[:_BYTES_DE_FIRMA])


def emitir_pase(
    evento_uuid: UUID, vigencia_segundos: int, llave: str | None = None
) -> str:
    """Lo llamara la sala al dar el turno. Hoy solo lo usan las pruebas."""
    llave = llave or settings.SECRET_KEY
    vence = int(time.time()) + vigencia_segundos
    cuerpo = (
        f"{VERSION}.{_b64(evento_uuid.bytes)}."
        f"{_b64(secrets.token_bytes(16))}.{_base36(vence)}"
    )
    return f"{cuerpo}.{_firma(cuerpo, llave)}"


def leer_pase(texto: str, llave: str | None = None) -> Pase:
    """El pase, si lo firmo Palco. No revisa vencimiento: eso es aparte."""
    llave = llave or settings.SECRET_KEY
    partes = texto.strip().split(".")
    if len(partes) != 5 or partes[0] != VERSION:
        raise PaseInvalido
    cuerpo = ".".join(partes[:4])
    if not hmac.compare_digest(partes[4], _firma(cuerpo, llave)):
        raise PaseInvalido
    try:
        evento = UUID(bytes=_de_b64(partes[1]))
        vence = int(partes[3], 36)
    except (ValueError, binascii.Error) as exc:
        raise PaseInvalido from exc
    return Pase(evento, partes[2], vence)


def clave_sala_activa(evento_uuid: UUID) -> str:
    return f"sala:activa:{evento_uuid}"


_USAR_PASE = """
if redis.call('SISMEMBER', KEYS[1], ARGV[1]) == 1 then return 1 end
if redis.call('SCARD', KEYS[1]) >= tonumber(ARGV[2]) then return 0 end
redis.call('SADD', KEYS[1], ARGV[1])
redis.call('EXPIREAT', KEYS[1], ARGV[3])
return 1
"""


async def verificar_turno(
    db: AsyncSession,
    redis: Redis,
    performance_id: int,
    hold_id: str,
    texto_del_pase: str | None,
) -> None:
    """Deja empezar un apartado solo con turno, si la sala esta activa."""
    if not settings.SALA_DE_ESPERA_HABILITADA:
        return
    evento_uuid = await db.scalar(
        select(Event.uuid)
        .join(Performance, Performance.event_id == Event.id)
        .where(Performance.id == performance_id)
    )
    if evento_uuid is None:
        return  # El apartado respondera que la funcion no existe.

    try:
        activa = await redis.exists(clave_sala_activa(evento_uuid))
    except RedisError:
        log.warning("Puerta del turno sin Redis: se deja pasar.")
        return
    if not activa:
        return

    # Quien ya estaba comprando sigue, sin pase.
    ya_tenia = await db.scalar(
        select(PerformanceSeat.id)
        .where(
            PerformanceSeat.performance_id == performance_id,
            PerformanceSeat.hold_id == hold_id,
        )
        .limit(1)
    )
    if ya_tenia is not None:
        return

    if not texto_del_pase:
        raise TurnoRequerido(MENSAJE_SALA)
    try:
        pase = leer_pase(texto_del_pase)
    except PaseInvalido:
        log.warning("Pase de turno invalido o alterado.")
        raise TurnoRequerido(MENSAJE_SALA) from None
    if pase.evento_uuid != evento_uuid:
        raise TurnoRequerido(MENSAJE_SALA)
    if pase.vence <= int(time.time()):
        raise TurnoVencido(
            "Tu turno para comprar terminó. Vuelve a la sala de espera para "
            "obtener uno nuevo."
        )

    try:
        ok = await redis.eval(
            _USAR_PASE,
            1,
            f"sala:pase:{pase.pase_id}",
            hold_id,
            APARTADOS_POR_PASE,
            pase.vence,
        )
    except RedisError:
        log.warning("Puerta del turno sin Redis: se deja pasar.")
        return
    if not ok:
        log.warning("Un pase de turno intento abrir mas apartados de la cuenta.")
        raise TurnoUsado(
            "Este turno ya se usó para otra compra. Vuelve a la sala de espera "
            "para obtener uno nuevo."
        )
