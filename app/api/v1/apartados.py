"""Endpoints del apartado: la primera vez que un comprador toca el sistema.

Son publicos. El comprador puede ser invitado y todavia no tiene sesion;
la credencial del apartado es el propio `hold_id`, un UUID4 imposible de
adivinar que genera el front.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, status
from pydantic import UUID4

from app.api.deps import DB
from app.schemas.apartados import ApartadoResponse, ApartarRequest
from app.services.apartados import ApartadoService
from app.services.holds import HoldService, hold_service
from app.services.limites import (
    RedisLimites,
    limite,
    registrar_apartado,
    soltar_apartado,
)

router = APIRouter(prefix="/performances", tags=["apartados"])


def get_hold_service() -> HoldService:
    """Dependencia para que las pruebas usen su propio Redis."""
    return hold_service


Holds = Annotated[HoldService, Depends(get_hold_service)]


@router.post(
    "/{performance_id}/apartados",
    response_model=ApartadoResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[limite("apartar")],
)
async def apartar(
    performance_id: int,
    datos: ApartarRequest,
    db: DB,
    holds: Holds,
    request: Request,
    redis: RedisLimites,
) -> ApartadoResponse:
    """Aparta butacas. Repetirla con el mismo `hold_id` es seguro.

    Si el commit falla despues de apartar en Redis, las llaves vencen
    solas con su TTL: molesto, no catastrofico.
    """
    await registrar_apartado(redis, request, datos.hold_id.hex)
    estado = await ApartadoService(db, holds).apartar(
        performance_id, datos.seat_ids, datos.hold_id.hex
    )
    await db.commit()
    return ApartadoResponse.model_validate(estado)


@router.get(
    "/{performance_id}/apartados/{hold_id}",
    response_model=ApartadoResponse,
    dependencies=[limite("consulta")],
)
async def consultar_apartado(
    performance_id: int, hold_id: UUID4, db: DB, holds: Holds
) -> ApartadoResponse:
    estado = await ApartadoService(db, holds).consultar(performance_id, hold_id.hex)
    return ApartadoResponse.model_validate(estado)


@router.delete(
    "/{performance_id}/apartados/{hold_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[limite("apartar")],
)
async def liberar_apartado(
    performance_id: int,
    hold_id: UUID4,
    db: DB,
    holds: Holds,
    request: Request,
    redis: RedisLimites,
    seat_id: Annotated[list[int] | None, Query()] = None,
) -> None:
    """Suelta todo el apartado, o solo las butacas en `seat_id`."""
    await ApartadoService(db, holds).liberar(performance_id, hold_id.hex, seat_id)
    await db.commit()
    if not seat_id:
        # Soltado completo: ya no cuenta contra el tope de su IP.
        await soltar_apartado(redis, request, hold_id.hex)
