"""Limites contra bots y fuerza bruta (B1, B2, B5 del plan; ADR 0016).

Tres defensas, todas en Redis y todas baratas:

- **Limite de velocidad** por IP y por grupo de rutas (`limite(...)`):
  ventana fija de 60 s. Responde 429 con `Retry-After`.
- **Tope de apartados vigentes** por IP: un bot que genera hold_id
  nuevos no puede acaparar el mapa. Y un tope de ordenes pendientes por
  correo en el checkout.
- **Candado de login** por correo: tras 5 fallos en 15 min, la cuenta no
  entra por 15 min aunque la contrasena sea correcta. Igual para correos
  que no existen, para no delatar cuales si.

Si Redis no responde, se deja pasar y se anota: estas defensas no deben
tumbar la venta. El apartado mismo depende de Redis y fallara por su
cuenta si Redis de verdad cayo.

Las IP compartidas (red de celular, oficinas) existen: los topes son
holgados a proposito. Contra bots de verdad la defensa principal es la
verificacion humana (B3) y el WAF (B4); esto es la red de seguridad.
"""

import logging
import time
from typing import Annotated

from fastapi import Depends, Request
from redis.asyncio.client import Redis
from redis.exceptions import RedisError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import ErrorDeDominio
from app.core.redis import redis_client
from app.models.sales import Customer, Order

log = logging.getLogger("palco.seguridad")

VENTANA = 60
# Solicitudes por minuto y por IP de cada grupo de rutas.
POR_MINUTO = {
    "login": 10,
    "apartar": 30,
    "checkout": 10,
    "confirmar": 20,
    "consulta": 60,
    "publico": 120,
    # Reenviar la liga de los boletos (BOL15). Ademas hay tope por compra.
    "reenviar": 6,
    # Pasar o recuperar un boleto (BOL6). Ademas hay tope por compra.
    "pasar": 10,
}
APARTADOS_VIGENTES_POR_IP = 4
ORDENES_PENDIENTES_POR_CORREO = 2
FALLOS_DE_LOGIN = 5
BLOQUEO_DE_LOGIN = 15 * 60


class DemasiadasSolicitudes(ErrorDeDominio):
    codigo = "demasiadas_solicitudes"
    http_status = 429

    def __init__(self, mensaje: str, reintentar_en: int) -> None:
        super().__init__(mensaje, {"reintentar_en_segundos": reintentar_en})
        self.headers = {"Retry-After": str(reintentar_en)}


class DemasiadosApartados(ErrorDeDominio):
    codigo = "demasiados_apartados"
    http_status = 429


class DemasiadasOrdenes(ErrorDeDominio):
    codigo = "demasiadas_ordenes"
    http_status = 409


class AccesoBloqueado(DemasiadasSolicitudes):
    codigo = "acceso_bloqueado"


def get_redis_limites() -> Redis:
    """Dependencia para que las pruebas usen su propio Redis."""
    return redis_client


RedisLimites = Annotated[Redis, Depends(get_redis_limites)]


def ip_de(request: Request) -> str:
    """La IP del cliente, sin creerle a un `X-Forwarded-For` falso.

    Detras del balanceador la IP real llega en `X-Forwarded-For`, que el
    cliente tambien puede escribir. Solo se toma la entrada que agrego el
    ultimo proxy de confianza, contando desde la derecha.
    """
    saltos = settings.PROXIES_DE_CONFIANZA
    if saltos > 0:
        cadena = [
            p.strip()
            for p in request.headers.get("x-forwarded-for", "").split(",")
            if p.strip()
        ]
        if len(cadena) >= saltos:
            return cadena[-saltos]
    return request.client.host if request.client else "desconocida"


_CONTAR = """
local n = redis.call('INCR', KEYS[1])
if n == 1 then redis.call('EXPIRE', KEYS[1], ARGV[1]) end
local ttl = redis.call('TTL', KEYS[1])
return {n, ttl}
"""


async def _contar(redis: Redis, clave: str, ventana: int) -> tuple[int, int]:
    n, ttl = await redis.eval(_CONTAR, 1, clave, ventana)
    return int(n), max(int(ttl), 1)


def limite(grupo: str):
    """Dependencia: N solicitudes por minuto por IP para este grupo."""
    tope = POR_MINUTO[grupo]

    async def _verificar(request: Request, redis: RedisLimites) -> None:
        if not settings.LIMITES_ACTIVOS:
            return
        clave = f"lim:{grupo}:{ip_de(request)}"
        try:
            n, ttl = await _contar(redis, clave, VENTANA)
        except RedisError:
            log.warning("Limite %s sin Redis: se deja pasar.", grupo)
            return
        if n > tope:
            if n == tope + 1:
                log.warning("Limite %s rebasado por una IP.", grupo)
            raise DemasiadasSolicitudes(
                "Recibimos demasiadas solicitudes desde tu conexión. "
                "Espera un minuto e intenta de nuevo.",
                ttl,
            )

    return Depends(_verificar)


# --------------------------------------------------------------------------
# Apartados vigentes por IP
# --------------------------------------------------------------------------

_APARTADO = """
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', ARGV[1])
if redis.call('ZSCORE', KEYS[1], ARGV[2]) then
  redis.call('ZADD', KEYS[1], ARGV[3], ARGV[2])
  return 1
end
if redis.call('ZCARD', KEYS[1]) >= tonumber(ARGV[4]) then
  return 0
end
redis.call('ZADD', KEYS[1], ARGV[3], ARGV[2])
redis.call('EXPIRE', KEYS[1], ARGV[5])
return 1
"""


async def registrar_apartado(redis: Redis, request: Request, hold_id: str) -> None:
    """Cuenta el apartado contra su IP, o rechaza si ya tiene demasiados.

    Repetir con un hold_id que ya cuenta siempre pasa: es el mismo
    comprador sumando butacas.
    """
    if not settings.LIMITES_ACTIVOS:
        return
    ahora = time.time()
    vida = settings.HOLD_TTL_SEGUNDOS + settings.HOLD_TTL_CHECKOUT_SEGUNDOS
    try:
        ok = await redis.eval(
            _APARTADO,
            1,
            f"lim:apartados:{ip_de(request)}",
            ahora,
            hold_id,
            ahora + vida,
            APARTADOS_VIGENTES_POR_IP,
            vida,
        )
    except RedisError:
        log.warning("Tope de apartados sin Redis: se deja pasar.")
        return
    if not ok:
        log.warning("Tope de apartados vigentes rebasado por una IP.")
        raise DemasiadosApartados(
            "Ya tienes varias compras en proceso desde esta conexión. "
            "Termina alguna o espera unos minutos para empezar otra."
        )


async def soltar_apartado(redis: Redis, request: Request, hold_id: str) -> None:
    if not settings.LIMITES_ACTIVOS:
        return
    try:
        await redis.zrem(f"lim:apartados:{ip_de(request)}", hold_id)
    except RedisError:
        log.warning("No se pudo descontar el apartado soltado.")


# --------------------------------------------------------------------------
# Candado de login
# --------------------------------------------------------------------------


def _clave_login(email: str) -> str:
    return f"lim:login-fallos:{email.strip().lower()}"


async def verificar_candado(redis: Redis, email: str) -> None:
    if not settings.LIMITES_ACTIVOS:
        return
    try:
        fallos = await redis.get(_clave_login(email))
        ttl = await redis.ttl(_clave_login(email))
    except RedisError:
        log.warning("Candado de login sin Redis: se deja pasar.")
        return
    if fallos is not None and int(fallos) >= FALLOS_DE_LOGIN:
        raise AccesoBloqueado(
            "Por seguridad bloqueamos el acceso a esta cuenta durante 15 minutos "
            "por varios intentos fallidos.",
            max(int(ttl), 1),
        )


async def anotar_fallo(redis: Redis, email: str) -> None:
    if not settings.LIMITES_ACTIVOS:
        return
    try:
        n, _ = await _contar(redis, _clave_login(email), BLOQUEO_DE_LOGIN)
    except RedisError:
        return
    if n == FALLOS_DE_LOGIN:
        # Alerta a PSD (A4). Se anota el dominio, no el correo completo.
        dominio = email.rsplit("@", 1)[-1]
        log.warning("Login bloqueado 15 min tras %d fallos (@%s).", n, dominio)


async def limpiar_fallos(redis: Redis, email: str) -> None:
    if not settings.LIMITES_ACTIVOS:
        return
    try:
        await redis.delete(_clave_login(email))
    except RedisError:
        return


# --------------------------------------------------------------------------
# Ordenes pendientes por correo
# --------------------------------------------------------------------------


async def verificar_ordenes_pendientes(
    db: AsyncSession, email: str, hold_id: str
) -> None:
    """Un correo no abre mas de N compras a la vez con apartados distintos.

    Volver a pagar el mismo apartado (doble clic, corregir datos) no
    cuenta: es la misma compra.
    """
    if not settings.LIMITES_ACTIVOS:
        return
    abiertas = await db.scalar(
        select(func.count())
        .select_from(Order)
        .join(Customer, Customer.id == Order.customer_id)
        .where(
            Customer.email == email.strip().lower(),
            Order.estado == "pendiente",
            Order.expira_en > func.now(),
            Order.hold_id != hold_id,
        )
    )
    if abiertas >= ORDENES_PENDIENTES_POR_CORREO:
        raise DemasiadasOrdenes(
            "Ya tienes compras en proceso con este correo. Termina alguna o "
            "espera unos minutos a que venzan para empezar otra."
        )
