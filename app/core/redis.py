"""Cliente Redis y carga de scripts Lua.

Los scripts se registran una sola vez al arranque. Redis los ejecuta de
forma atomica: ningun otro comando corre mientras el script esta en curso.
Esa es la garantia que sostiene el apartado de butacas.
"""

from pathlib import Path
from typing import ClassVar

import redis.asyncio as aioredis
from redis.asyncio.client import Redis
from redis.commands.core import AsyncScript

from app.core.config import settings

_LUA_DIR = Path(__file__).resolve().parent.parent / "services" / "lua"

# BlockingConnectionPool y no el pool normal: cuando se agota, el normal
# levanta MaxConnectionsError de inmediato. Bajo una avalancha de compra
# eso convierte una espera de milisegundos en un error para el usuario.
# El bloqueante forma a quien llega y atiende en orden, que es justo el
# comportamiento que queremos el dia que abre una venta.
_pool = aioredis.BlockingConnectionPool.from_url(
    str(settings.REDIS_URL),
    encoding="utf-8",
    decode_responses=True,
    max_connections=settings.REDIS_MAX_CONNECTIONS,
    timeout=settings.REDIS_POOL_TIMEOUT,
)

redis_client: Redis = aioredis.Redis(connection_pool=_pool)


def _load(nombre: str) -> AsyncScript:
    ruta = _LUA_DIR / f"{nombre}.lua"
    return redis_client.register_script(ruta.read_text(encoding="utf-8"))


class LuaScripts:
    """Scripts cargados perezosamente para no tocar Redis al importar."""

    _cache: ClassVar[dict[str, AsyncScript]] = {}

    @classmethod
    def get(cls, nombre: str) -> AsyncScript:
        if nombre not in cls._cache:
            cls._cache[nombre] = _load(nombre)
        return cls._cache[nombre]


async def ping() -> bool:
    try:
        return await redis_client.ping()
    except Exception:
        return False
