"""Proceso del barrido.

    python -m app.workers.barrido            # cada 60 s, hasta Ctrl+C
    python -m app.workers.barrido --una-vez  # una vuelta y termina

En produccion corre como un servicio aparte de la API (ECS), con una sola
tarea. Aun asi se protege con un candado consultivo de Postgres: si por
error arrancan dos, la segunda espera su turno en vez de barrer al mismo
tiempo. El candado es de la conexion: si el proceso muere, Postgres lo
suelta solo.
"""

from __future__ import annotations

import argparse
import asyncio
import logging

from sqlalchemy import text

from app.core.db import SessionLocal, engine
from app.core.redis import redis_client
from app.services.barrido import BarridoService
from app.services.holds import HoldService
from app.services.latidos import latir
from app.services.pasarela import pasarela_de_stripe

log = logging.getLogger("palco.barrido")

# Identificador del candado consultivo. Cualquier numero fijo y unico.
CANDADO = 510_051


async def una_vuelta() -> None:
    servicio = BarridoService(
        SessionLocal, HoldService(redis_client), pasarela_de_stripe()
    )
    async with engine.connect() as conn:
        tengo = await conn.scalar(
            text("SELECT pg_try_advisory_lock(:k)"), {"k": CANDADO}
        )
        if not tengo:
            log.info("Otro barrido esta corriendo; esta vuelta se salta.")
            return
        try:
            r = await servicio.una_vuelta()
        finally:
            await conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": CANDADO})
    # Solo late quien de verdad barrio: el que se salto la vuelta no cuenta.
    await latir(redis_client, "barrido")
    if r.hubo_algo():
        log.info("Barrido: %s", r)


async def main(una_vez: bool, cada: int) -> None:
    try:
        while True:
            try:
                await una_vuelta()
            except Exception:
                # Postgres o Redis caidos un momento: se reintenta en la
                # siguiente vuelta, el proceso no muere.
                log.exception("Barrido: la vuelta fallo")
            if una_vez:
                return
            await asyncio.sleep(cada)
    finally:
        await redis_client.aclose()
        await engine.dispose()


def _argumentos() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Barrido de apartados y ordenes")
    p.add_argument("--una-vez", action="store_true", help="una vuelta y termina")
    p.add_argument("--cada", type=int, default=60, help="segundos entre vueltas")
    return p.parse_args()


if __name__ == "__main__":
    a = _argumentos()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    asyncio.run(main(a.una_vez, a.cada))
