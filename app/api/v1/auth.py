"""Endpoints de autenticacion del personal.

Recuperar la contrasena: un codigo al correo (el mismo de la cuenta del
comprador, con otro proposito). La respuesta es la misma exista o no la
cuenta, para no decir que correos son del equipo. Al cambiarla se cierran
sus sesiones y queda probado que el correo es suyo.
"""

from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Header, Request, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import func, select

from app.api.deps import DB, UsuarioActual
from app.api.v1.cuenta import (
    CarteroCuenta,
    CodigoEnviado,
    RedisCuenta,
    _mandar,
    por_ip,
)
from app.core.config import settings
from app.core.security import (
    CredencialesInvalidas,
    crear_access_token,
    hash_password,
)
from app.models.users import User
from app.schemas.auth import (
    CambioPasswordRequest,
    LoginRequest,
    TokenResponse,
    UsuarioResponse,
)
from app.services import cuenta as svc
from app.services.auth import AuthService
from app.services.limites import (
    RedisLimites,
    anotar_fallo,
    limite,
    limpiar_fallos,
    verificar_candado,
)
from app.services.reenvio import tapar_correo
from app.services.verificacion import Verificacion, verificar_persona

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
        subject=usuario.id,
        rol=usuario.rol,
        client_id=usuario.client_id,
        version=usuario.sesion_version,
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


# --------------------------------------------------------------------------
# Recuperar la contrasena
# --------------------------------------------------------------------------


class RecuperarRequest(BaseModel):
    email: EmailStr


class NuevaPasswordRequest(BaseModel):
    email: EmailStr
    codigo: str = Field(min_length=1, max_length=12)
    password_nueva: str = Field(min_length=10, max_length=128)


@router.post(
    "/recuperar",
    response_model=CodigoEnviado,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[por_ip],
)
async def recuperar(
    datos: RecuperarRequest,
    request: Request,
    db: DB,
    tareas: BackgroundTasks,
    redis: RedisCuenta,
    cartero: CarteroCuenta,
    verificador: Verificacion,
    verificacion: Annotated[str | None, Header(alias="X-Palco-Verificacion")] = None,
) -> CodigoEnviado:
    """Manda un codigo para cambiar la contrasena. Responde igual si el
    correo no es del equipo (solo que entonces no sale ningun correo)."""
    await verificar_persona(verificador, request, verificacion)
    correo = svc.normalizar_correo(str(datos.email))
    # Los topes por correo cuentan igual, sea o no del equipo.
    codigo = await svc.nuevo_codigo(redis, svc.CONTRASENA, correo)
    activo = await db.scalar(
        select(User.id).where(User.email == correo, User.activo.is_(True))
    )
    if activo is not None:
        tareas.add_task(
            _mandar, cartero, svc.correo_del_codigo(correo, codigo, svc.CONTRASENA)
        )
    return CodigoEnviado(
        enviado_a=tapar_correo(correo), minutos=svc.VIDA_DEL_CODIGO // 60
    )


@router.post(
    "/recuperar/confirmar",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[por_ip],
)
async def nueva_password(
    datos: NuevaPasswordRequest, db: DB, redis: RedisCuenta, limites: RedisLimites
) -> None:
    correo = svc.normalizar_correo(str(datos.email))
    await svc.comprobar_codigo(redis, svc.CONTRASENA, correo, datos.codigo)
    usuario = await db.scalar(
        select(User).where(User.email == correo, User.activo.is_(True))
    )
    if usuario is None:
        # Codigo correcto de un correo que no es del equipo: igual que vencido.
        raise svc.CodigoInvalido(
            "Ese código ya venció o se usaron todos sus intentos. Pide uno nuevo."
        )
    usuario.password_hash = hash_password(datos.password_nueva.strip())
    usuario.sesion_version += 1
    if usuario.email_verificado_en is None:
        usuario.email_verificado_en = func.now()
    await db.commit()
    # Si estaba bloqueada por intentos fallidos, ya puede entrar.
    await limpiar_fallos(limites, correo)
