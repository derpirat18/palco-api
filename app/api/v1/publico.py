"""Endpoints publicos: lo que ve el comprador sin iniciar sesion."""

import contextlib
import json
import logging
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Path, Query
from pydantic import UUID4
from redis.asyncio.client import Redis
from redis.exceptions import RedisError

from app.api.deps import DB
from app.core.config import settings
from app.core.errors import RecursoNoEncontrado
from app.core.redis import redis_client
from app.schemas.publico import (
    CarteleraResponse,
    ConfiguracionPublicaResponse,
    CorreoEnElNavegadorResponse,
    EventoPublicoResponse,
    MapaPublicoResponse,
)
from app.services.cartelera import Busqueda, CarteleraService, clave_de_visitas
from app.services.confirmacion import armar_confirmacion
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
    "/configuracion",
    response_model=ConfiguracionPublicaResponse,
    dependencies=[limite("publico")],
)
async def configuracion_publica() -> dict:
    """Llaves publicas por entorno: el front no las lleva escritas.

    La sitekey de Turnstile es distinta en sandbox y en produccion; asi el
    mismo front sirve en los dos.
    """
    site_key = settings.TURNSTILE_SITE_KEY or None
    return {
        "verificacion_humana": {"activa": site_key is not None, "site_key": site_key}
    }


@router.get(
    "/eventos/{slug}",
    response_model=EventoPublicoResponse,
    dependencies=[limite("publico")],
)
async def evento_publico(
    slug: Annotated[str, Path(max_length=160)],
    db: DB,
    redis: Annotated[Redis, Depends(get_redis_publico)],
) -> EventoPublicoResponse:
    """La pagina del evento por su link de compra (psd.com/<slug>).

    Un evento en borrador, de un organizador dado de baja o inexistente
    responden igual: 404.

    Cada visita suma para "lo mas buscado" de la cartelera. Si Redis no
    responde, la pagina se muestra igual.
    """
    evento = await PublicoService(db).evento_por_slug(slug)
    await _contar_visita(redis, evento.uuid)
    return EventoPublicoResponse.model_validate(evento)


async def _contar_visita(redis: Redis, evento_uuid: UUID) -> None:
    """El conteo solo ordena "lo mas buscado": ninguna falla al contar
    (Redis caido, lento o mal configurado) puede tumbar la pagina que vende.
    """
    try:
        await redis.incr(clave_de_visitas(evento_uuid))
    except Exception:
        log.warning("No se conto la visita del evento %s.", evento_uuid)


@router.get(
    "/cartelera",
    response_model=CarteleraResponse,
    dependencies=[limite("publico")],
)
async def cartelera(
    db: DB,
    redis: Annotated[Redis, Depends(get_redis_publico)],
    q: Annotated[
        str | None, Query(max_length=80, description="Evento, recinto o ciudad")
    ] = None,
    pais: Annotated[str | None, Query(min_length=2, max_length=2)] = None,
    ciudad: Annotated[str | None, Query(max_length=80)] = None,
    mes: Annotated[str | None, Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")] = None,
    categoria: Annotated[str | None, Query(max_length=60)] = None,
    destacados: bool = False,
    orden: Literal["fecha", "populares"] = "fecha",
    limite_: Annotated[int, Query(alias="limite", ge=1, le=100)] = 50,
) -> CarteleraResponse:
    """Los eventos que se pueden comprar, con busqueda y filtros.

    Solo eventos publicados de organizadores activos, con alguna funcion
    proxima. El texto ignora acentos y mayusculas. `orden=populares` es
    "lo mas buscado", por visitas a la pagina del evento.
    """
    servicio = CarteleraService(db)
    visitas: dict[UUID, int] = {}
    if orden == "populares":
        uuids = await servicio.visibles()
        try:
            valores = (
                await redis.mget([clave_de_visitas(u) for u in uuids]) if uuids else []
            )
            visitas = {u: int(v) for u, v in zip(uuids, valores, strict=True) if v}
        except Exception:
            # Igual que el conteo: sin visitas se ordena por fecha.
            log.warning("Cartelera sin Redis: se ordena por fecha.")
    resultado = await servicio.cartelera(
        Busqueda(
            texto=q,
            pais=pais,
            ciudad=ciudad,
            mes=mes,
            categoria=categoria,
            solo_destacados=destacados,
            orden=orden,
            limite=limite_,
        ),
        visitas,
    )
    return CarteleraResponse.model_validate(resultado)


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


@router.get(
    "/ordenes/{orden_uuid}/correo",
    response_model=CorreoEnElNavegadorResponse,
    dependencies=[limite("consulta")],
)
async def correo_en_el_navegador(orden_uuid: UUID, db: DB) -> dict:
    """El correo de confirmacion tal como se mando ("Ver en el navegador").

    La llave es el uuid de la orden, como "Tus boletos". El correo no trae
    QR ni codigos: no da nada que la liga de la compra no de ya.
    """
    mensaje = await armar_confirmacion(db, orden_uuid, para_enviar=False)
    if mensaje is None:
        raise RecursoNoEncontrado("Este correo ya no está disponible.")
    return {"asunto": mensaje.asunto, "html": mensaje.html}
