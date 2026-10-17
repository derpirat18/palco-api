"""Latidos de los procesos de fondo (A3).

El barrido y la bandeja corren aparte de la API. Si se caen, la API sigue
respondiendo y nadie se entera, pero las ordenes vencidas no se cierran y
los correos con boletos no salen. Por eso cada vuelta deja su marca en
Redis, y `/health/trabajadores` responde 503 si alguno lleva demasiado sin
dar vuelta. El monitor externo (A2) consulta esa ruta y avisa a PSD.

Va en una ruta propia y no en `/health/ready` a proposito: si el
balanceador usara esa, un barrido caido sacaria a la API de servicio.
"""

import logging
import time

from redis.asyncio.client import Redis
from redis.exceptions import RedisError

log = logging.getLogger("palco.latidos")

# Cuanto puede pasar sin latido antes de dar la alarma: unas tres vueltas.
TOLERANCIA_SEGUNDOS = {"barrido": 180, "bandeja": 60}
# La marca se borra sola si nadie la renueva: un proceso apagado de hace
# dias no debe parecer vivo por un valor viejo.
_VIDA = 24 * 3600


def clave(nombre: str) -> str:
    return f"latido:{nombre}"


async def latir(redis: Redis, nombre: str) -> None:
    """Lo llama cada proceso al terminar una vuelta."""
    try:
        await redis.set(clave(nombre), int(time.time()), ex=_VIDA)
    except RedisError:
        log.warning("No se pudo registrar el latido de %s.", nombre)


async def estado_de_latidos(redis: Redis) -> dict[str, dict]:
    """Por proceso: si esta al dia y hace cuantos segundos dio su ultima vuelta."""
    ahora = int(time.time())
    estado = {}
    for nombre, tolerancia in TOLERANCIA_SEGUNDOS.items():
        try:
            valor = await redis.get(clave(nombre))
        except RedisError:
            valor = None
        hace = ahora - int(valor) if valor is not None else None
        estado[nombre] = {
            "al_dia": hace is not None and hace <= tolerancia,
            "hace_segundos": hace,
        }
    return estado
