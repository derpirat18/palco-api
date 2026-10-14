"""Esquemas de autenticacion."""

from pydantic import BaseModel, EmailStr, Field


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expira_en_segundos: int


class UsuarioResponse(BaseModel):
    id: int
    email: str
    nombre: str
    rol: str
    client_id: int | None

    model_config = {"from_attributes": True}


class CambioPasswordRequest(BaseModel):
    password_actual: str = Field(min_length=8, max_length=128)
    password_nueva: str = Field(min_length=10, max_length=128)
