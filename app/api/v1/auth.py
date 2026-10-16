"""Endpoints de autenticacion del personal."""

from fastapi import APIRouter, status

from app.api.deps import DB, UsuarioActual
from app.core.config import settings
from app.core.security import CredencialesInvalidas, crear_access_token
from app.schemas.auth import (
    CambioPasswordRequest,
    LoginRequest,
    TokenResponse,
    UsuarioResponse,
)
from app.services.auth import AuthService
from app.services.limites import (
    RedisLimites,
    anotar_fallo,
    limite,
    limpiar_fallos,
    verificar_candado,
)

router = APIRouter(prefix="/auth")


@router.post("/login", response_model=TokenResponse, dependencies=[limite("login")])
async def login(datos: LoginRequest, db: DB, redis: RedisLimites) -> TokenResponse:
    # El candado va antes de revisar la contrasena: bloqueada, ni la
    # correcta entra. Asi adivinar no sirve aunque se acierte.
    await verificar_candado(redis, datos.email)
    try:
        usuario = await AuthService(db).autenticar(datos.email, datos.password)
    except CredencialesInvalidas:
        await anotar_fallo(redis, datos.email)
        raise
    await limpiar_fallos(redis, datos.email)
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
