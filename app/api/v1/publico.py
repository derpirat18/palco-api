"""Endpoints publicos: lo que ve el comprador sin iniciar sesion."""

import contextlib
import json
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query
from pydantic import UUID4
from redis.asyncio.client import Redis
from redis.exceptions import RedisError

from app.api.deps import DB
from app.core.redis import redis_client
from app.schemas.publico import EventoPublicoResponse, MapaPublicoResponse
from app.services.limites import limite
from app.services.publico import PublicoService

router = APIRouter(prefix="/publico", tags=["publico"])
log = logging.getLogger("palco.publico")

# El dia de una venta grande, miles consultan el mismo mapa a la vez. Con
# este cache, mil consultas en el mismo segundo cuestan una a la base. No
# causa sobreventa: quien elige una butaca que se acaba de ocupar recibe
# "Alguien mas acaba de elegir ese lugar" al apartar.
CACHE_DEL_MAPA_MS = 2000


def get_redis_publico() -> Redis:
    """Dependencia para que las pruebas usen su propio Redis."""
    return redis_client


@router.get(
    "/eventos/{slug}",
    response_model=EventoPublicoResponse,
    dependencies=[limite("publico")],
)
async def evento_publico(
    slug: Annotated[str, Path(max_length=160)], db: DB
) -> EventoPublicoResponse:
    """La pagina del evento por su link de compra (psd.com/<slug>).

    Un evento en borrador, de un organizador dado de baja o inexistente
    responden igual: 404.
    """
    evento = await PublicoService(db).evento_por_slug(slug)
    return EventoPublicoResponse.model_validate(evento)


@router.get(
    "/funciones/{performance_id}/mapa",
    response_model=MapaPublicoResponse,
    dependencies=[limite("publico")],
)
async def mapa_de_funcion(
    performance_id: int,
    db: DB,
    redis: Annotated[Redis, Depends(get_redis_publico)],
    apartado: Annotated[UUID4 | None, Query()] = None,
) -> dict:
    """El mapa de butacas de la funcion, con precio final y disponibilidad.

    Con `apartado` (el hold_id del comprador), sus butacas vienen como
    "tuya": al recargar la pagina no pierde de vista lo que ya eligio.
    """
    servicio = PublicoService(db)
    clave = f"mapa:{performance_id}"
    cuerpo = None
    try:
        guardado = await redis.get(clave)
        cuerpo = json.loads(guardado) if guardado else None
    except RedisError:
        log.warning("Cache del mapa sin Redis: se consulta la base.")
    if cuerpo is None:
        mapa = await servicio.mapa_de_funcion(performance_id)
        cuerpo = MapaPublicoResponse.model_validate(mapa).model_dump(mode="json")
        with contextlib.suppress(RedisError):
            await redis.set(clave, json.dumps(cuerpo), px=CACHE_DEL_MAPA_MS)

    if apartado is not None:
        mias = await servicio.butacas_del_apartado(performance_id, apartado.hex)
        for b in cuerpo["butacas"]:
            if b["id"] in mias:
                b["estado"] = "tuya"
    return cuerpo
