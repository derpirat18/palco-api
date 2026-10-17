"""Esquemas de cortesias (C1)."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field


class CortesiaRequest(BaseModel):
    # Ids de butaca del mapa (los mismos del mapa publico).
    seat_ids: list[int] = Field(min_length=1, max_length=20)
    email: EmailStr
    nombre: str = Field(min_length=1, max_length=120)
    # Prensa, patrocinador, familia del elenco...: queda en la bitacora.
    motivo: str = Field(min_length=1, max_length=120)


class LugarDeCortesiaResponse(BaseModel):
    zona: str
    asiento: str


class CortesiaResponse(BaseModel):
    uuid: UUID
    folio: str
    # pagada: vigente. cancelada: revocada.
    estado: str
    email: str
    nombre: str | None
    motivo: str
    otorgada_en: datetime
    revocada_en: datetime | None
    lugares: list[LugarDeCortesiaResponse]
