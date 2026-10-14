"""Sondas de salud. Las usa el ALB y el despliegue."""

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.redis import ping as redis_ping

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
