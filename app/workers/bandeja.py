"""Proceso de la bandeja de salida: manda los correos de boletos.

    python -m app.workers.bandeja            # cada 5 s, hasta Ctrl+C
    python -m app.workers.bandeja --una-vez  # una vuelta y termina

En produccion corre como servicio aparte de la API. A diferencia del
barrido no necesita candado: cada hecho se toma con SKIP LOCKED, asi que
dos procesos a la vez se reparten el trabajo sin repetirlo.
"""

from __future__ import annotations

import argparse
import asyncio
import logging

from app.core.db import SessionLocal, engine
from app.core.redis import redis_client
from app.services.bandeja import BandejaService
from app.services.correo import cartero_por_configuracion
from app.services.latidos import latir

log = logging.getLogger("palco.bandeja")


async def main(una_vez: bool, cada: int) -> None:
    servicio = BandejaService(SessionLocal, cartero_por_configuracion())
    try:
        while True:
            try:
                r = await servicio.una_vuelta()
                if r.hubo_algo():
                    log.info("Bandeja: %s", r)
                await latir(redis_client, "bandeja")
            except Exception:
                # Base caida un momento: se reintenta en la siguiente vuelta.
                log.exception("Bandeja: la vuelta fallo")
            if una_vez:
                return
            await asyncio.sleep(cada)
    finally:
        await redis_client.aclose()
        await engine.dispose()


def _argumentos() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Bandeja de salida: correos de boletos")
    p.add_argument("--una-vez", action="store_true", help="una vuelta y termina")
    p.add_argument("--cada", type=int, default=5, help="segundos entre vueltas")
    return p.parse_args()


if __name__ == "__main__":
    a = _argumentos()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    asyncio.run(main(a.una_vez, a.cada))
