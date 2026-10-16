"""Endpoints publicos: lo que ve el comprador sin iniciar sesion."""

from typing import Annotated

from fastapi import APIRouter, Path

from app.api.deps import DB
from app.schemas.publico import EventoPublicoResponse
from app.services.limites import limite
from app.services.publico import PublicoService

router = APIRouter(prefix="/publico", tags=["publico"])


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
