"""Esquemas de cortesias (C1)."""

from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field

from app.core.textos import Linea, NombreDePersona


class CortesiaRequest(BaseModel):
    # Ids de butaca del mapa (los mismos del mapa publico).
    seat_ids: list[int] = Field(min_length=1, max_length=20)
    email: EmailStr
    # Sale en la lista de la puerta como titular.
    nombre: NombreDePersona = Field(min_length=1, max_length=120)
    # Prensa, patrocinador, familia del elenco...: queda en la bitacora.
    motivo: Linea = Field(min_length=1, max_length=120)
    # Opcional. Se normaliza a E.164 en el servicio (normalizar_telefono).
    telefono: Linea | None = Field(default=None, max_length=30)


class LugarDeCortesiaResponse(BaseModel):
    zona: str
    asiento: str
    # Para el panel (C4): el codigo impreso y si ya entro. Revocar una
    # cortesia con un boleto que ya entro no se puede.
    codigo: str | None = None
    entro: bool = False


class CortesiaResponse(BaseModel):
    uuid: UUID
    folio: str
    # pagada: vigente. cancelada: revocada.
    estado: str
    email: str
    nombre: str | None
    telefono: str | None = None
    motivo: str
    otorgada_en: datetime
    revocada_en: datetime | None
    lugares: list[LugarDeCortesiaResponse]


class ZonaDelPanelResponse(BaseModel):
    id: int
    nombre: str
    color: str | None
    precio_desde: Decimal | None

    model_config = {"from_attributes": True}


class ButacaDelPanelResponse(BaseModel):
    id: int
    zona_id: int
    fila: str
    numero: str
    x: Decimal
    y: Decimal
    # libre: se puede dar como cortesia.
    estado: Literal["libre", "ocupada"]
    situacion: str
    precio: Decimal

    model_config = {"from_attributes": True}


class MapaDelPanelResponse(BaseModel):
    """Solo para PSD y el organizador dueño: dice en qué está cada butaca."""

    viewport: dict
    zonas: list[ZonaDelPanelResponse]
    butacas: list[ButacaDelPanelResponse]

    model_config = {"from_attributes": True}
