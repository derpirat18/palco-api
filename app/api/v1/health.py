"""Sondas de salud. Las usa el ALB y el despliegue."""

from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from redis.asyncio.client import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.redis import ping as redis_ping
from app.core.redis import redis_client
from app.core.respuestas import JSONUtf8
from app.services.latidos import estado_de_latidos

router = APIRouter()


@router.get("/health")
async def health() -> dict:
    """Sonda superficial: responde si el proceso vive."""
    return {"estado": "ok"}


@router.get("/health/ready")
async def ready(db: AsyncSession = Depends(get_db)) -> dict:
    """Sonda profunda: verifica dependencias."""
    try:
        await db.execute(text("SELECT 1"))
        db_ok = True
    except Exception:
        db_ok = False

    redis_ok = await redis_ping()
    return {
        "estado": "ok" if (db_ok and redis_ok) else "degradado",
        "db": db_ok,
        "redis": redis_ok,
    }


def get_redis_salud() -> Redis:
    """Dependencia para que las pruebas usen su propio Redis."""
    return redis_client


@router.get("/health/trabajadores")
async def trabajadores(
    redis: Annotated[Redis, Depends(get_redis_salud)],
) -> JSONResponse:
    """Barrido y bandeja: 200 si los dos dan vueltas, 503 si alguno se paro.

    Para el monitor externo, no para el balanceador: la API sirve aunque un
    proceso de fondo este caido.
    """
    procesos = await estado_de_latidos(redis)
    al_dia = all(p["al_dia"] for p in procesos.values())
    return JSONUtf8(
        status_code=200 if al_dia else 503,
        content={"estado": "ok" if al_dia else "detenido", "procesos": procesos},
    )
