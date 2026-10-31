"""Esquemas del apartado de butacas."""

from datetime import datetime
from decimal import Decimal

from pydantic import UUID4, BaseModel, Field


class ApartarRequest(BaseModel):
    # Lo genera el front antes del primer clic y lo manda en cada intento.
    # Si lo generara el servidor, un reintento de la primera peticion
    # llegaria sin id, recibiria otro, y chocaria contra si mismo.
    hold_id: UUID4
    # Guarda gruesa contra cuerpos enormes. El tope real por compra lo
    # aplica el servicio, sumando lo que el apartado ya tenia.
    seat_ids: list[int] = Field(min_length=1, max_length=50)


class ApartarGeneralRequest(BaseModel):
    """Entrada general (EG): cuantos cupos mas de una zona sin lugares."""

    hold_id: UUID4
    zone_id: int
    cantidad: int = Field(ge=1, le=50)


class ButacaApartadaResponse(BaseModel):
    seat_id: int
    performance_seat_id: int
    precio: Decimal

    model_config = {"from_attributes": True}


class ApartadoResponse(BaseModel):
    # Se guarda en hexadecimal; se devuelve con guiones, igual que llego.
    hold_id: UUID4
    performance_id: int
    butacas: list[ButacaApartadaResponse]
    expira_en: datetime
    segundos_restantes: int

    model_config = {"from_attributes": True}
