"""Esquemas de las cuentas del personal (panel)."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, EmailStr, Field

from app.core.textos import Linea


class CuentaCrear(BaseModel):
    email: EmailStr
    nombre: Linea = Field(min_length=2, max_length=120)
    # admin_psd no: esas cuentas se crean con el script.
    rol: Literal["organizador", "acceso"]
    client_id: int


class CuentaActualizar(BaseModel):
    nombre: Linea | None = Field(default=None, min_length=2, max_length=120)
    activo: bool | None = None


class CuentaResponse(BaseModel):
    """Nunca lleva la contrasena ni su hash."""

    id: int
    email: str
    nombre: str
    rol: str
    client_id: int | None
    activo: bool
    ultimo_acceso: datetime | None

    model_config = {"from_attributes": True}


class CuentaConPasswordResponse(BaseModel):
    """La unica respuesta con la contrasena temporal: al crear o restablecer."""

    cuenta: CuentaResponse
    password_temporal: str


class AsignacionesRequest(BaseModel):
    """Los eventos donde va a escanear la cuenta. Reemplaza los anteriores."""

    event_ids: list[int] = Field(max_length=200)


class EventoAsignadoResponse(BaseModel):
    event_id: int
    nombre: str
    estado: str
    asignado_en: datetime
