"""Endpoints de autenticacion del personal."""

from fastapi import APIRouter, status

from app.api.deps import DB, UsuarioActual
from app.core.config import settings
from app.core.security import crear_access_token
from app.schemas.auth import (
    CambioPasswordRequest,
    LoginRequest,
    TokenResponse,
    UsuarioResponse,
)
from app.services.auth import AuthService

router = APIRouter(prefix="/auth")


@router.post("/login", response_model=TokenResponse)
async def login(datos: LoginRequest, db: DB) -> TokenResponse:
    usuario = await AuthService(db).autenticar(datos.email, datos.password)
    token = crear_access_token(
        subject=usuario.id, rol=usuario.rol, client_id=usuario.client_id
    )
    return TokenResponse(
        access_token=token,
        expira_en_segundos=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )


@router.get("/me", response_model=UsuarioResponse)
async def me(usuario: UsuarioActual) -> UsuarioResponse:
    return UsuarioResponse.model_validate(usuario)


@router.post("/password", status_code=status.HTTP_204_NO_CONTENT)
async def cambiar_password(
    datos: CambioPasswordRequest, usuario: UsuarioActual, db: DB
) -> None:
    await AuthService(db).cambiar_password(
        usuario, datos.password_actual, datos.password_nueva
    )
