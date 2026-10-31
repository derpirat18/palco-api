"""Endpoints del apartado: la primera vez que un comprador toca el sistema.

Son publicos. El comprador puede ser invitado y todavia no tiene sesion;
la credencial del apartado es el propio `hold_id`, un UUID4 imposible de
adivinar que genera el front.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Header, Query, Request, status
from pydantic import UUID4

from app.api.deps import DB
from app.schemas.apartados import (
    ApartadoResponse,
    ApartarGeneralRequest,
    ApartarRequest,
)
from app.services.apartados import ApartadoService
from app.services.holds import HoldService, hold_service
from app.services.limites import (
    RedisLimites,
    limite,
    registrar_apartado,
    soltar_apartado,
)
from app.services.turnos import verificar_turno
from app.services.verificacion import Verificacion, verificar_humano

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
    verificador: Verificacion,
    turno: Annotated[str | None, Header(alias="X-Palco-Turno")] = None,
    verificacion: Annotated[str | None, Header(alias="X-Palco-Verificacion")] = None,
) -> ApartadoResponse:
    """Aparta butacas. Repetirla con el mismo `hold_id` es seguro.

    Si el commit falla despues de apartar en Redis, las llaves vencen
    solas con su TTL: molesto, no catastrofico.

    Con la sala de espera activa, empezar un apartado exige el pase del
    turno en `X-Palco-Turno` (ADR 0017).

    Empezar un apartado exige tambien la verificacion humana de Turnstile
    en `X-Palco-Verificacion` (ADR 0019).
    """
    await verificar_turno(db, redis, performance_id, datos.hold_id.hex, turno)
    await verificar_humano(redis, verificador, request, datos.hold_id.hex, verificacion)
    await registrar_apartado(redis, request, datos.hold_id.hex)
    estado = await ApartadoService(db, holds).apartar(
        performance_id, datos.seat_ids, datos.hold_id.hex
    )
    await db.commit()
    return ApartadoResponse.model_validate(estado)


@router.post(
    "/{performance_id}/apartados/general",
    response_model=ApartadoResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[limite("apartar")],
)
async def apartar_general(
    performance_id: int,
    datos: ApartarGeneralRequest,
    db: DB,
    holds: Holds,
    request: Request,
    redis: RedisLimites,
    verificador: Verificacion,
    turno: Annotated[str | None, Header(alias="X-Palco-Turno")] = None,
    verificacion: Annotated[str | None, Header(alias="X-Palco-Verificacion")] = None,
) -> ApartadoResponse:
    """Entrada general (EG): aparta cupos de una zona sin lugares numerados.
    Las mismas guardas que apartar butacas (turno, verificacion, tope)."""
    await verificar_turno(db, redis, performance_id, datos.hold_id.hex, turno)
    await verificar_humano(redis, verificador, request, datos.hold_id.hex, verificacion)
    await registrar_apartado(redis, request, datos.hold_id.hex)
    estado = await ApartadoService(db, holds).apartar_general(
        performance_id, datos.zone_id, datos.cantidad, datos.hold_id.hex
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
