"""Esquemas de las cuentas del personal (panel)."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, EmailStr, Field


class CuentaCrear(BaseModel):
    email: EmailStr
    nombre: str = Field(min_length=2, max_length=120)
    # admin_psd no: esas cuentas se crean con el script.
    rol: Literal["organizador", "acceso"]
    client_id: int


class CuentaActualizar(BaseModel):
    nombre: str | None = Field(default=None, min_length=2, max_length=120)
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
