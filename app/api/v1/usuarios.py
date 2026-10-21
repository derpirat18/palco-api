"""Cuentas del personal desde el panel. Solo PSD."""

from fastapi import APIRouter, Depends, Query, status

from app.api.deps import DB, requiere_rol
from app.models.base import RolUsuario
from app.schemas.usuarios import (
    CuentaActualizar,
    CuentaConPasswordResponse,
    CuentaCrear,
    CuentaResponse,
)
from app.services.usuarios import UsuariosService

router = APIRouter(
    prefix="/usuarios",
    tags=["usuarios"],
    dependencies=[Depends(requiere_rol(RolUsuario.ADMIN_PSD))],
)


@router.get("", response_model=list[CuentaResponse])
async def listar_cuentas(
    db: DB, client_id: int | None = Query(default=None)
) -> list[CuentaResponse]:
    return [
        CuentaResponse.model_validate(u)
        for u in await UsuariosService(db).listar(client_id)
    ]


@router.post(
    "",
    response_model=CuentaConPasswordResponse,
    status_code=status.HTTP_201_CREATED,
)
async def crear_cuenta(datos: CuentaCrear, db: DB) -> CuentaConPasswordResponse:
    """La contrasena temporal sale solo en esta respuesta."""
    usuario, password = await UsuariosService(db).crear(
        datos.email, datos.nombre, datos.rol, datos.client_id
    )
    await db.commit()
    await db.refresh(usuario)
    return CuentaConPasswordResponse(
        cuenta=CuentaResponse.model_validate(usuario), password_temporal=password
    )


@router.patch("/{usuario_id}", response_model=CuentaResponse)
async def actualizar_cuenta(
    usuario_id: int, datos: CuentaActualizar, db: DB
) -> CuentaResponse:
    usuario = await UsuariosService(db).actualizar(
        usuario_id, datos.nombre, datos.activo
    )
    await db.commit()
    await db.refresh(usuario)
    return CuentaResponse.model_validate(usuario)


@router.post("/{usuario_id}/restablecer", response_model=CuentaConPasswordResponse)
async def restablecer_password(usuario_id: int, db: DB) -> CuentaConPasswordResponse:
    """Para quien olvido su contrasena: una nueva temporal, que se muestra una vez."""
    usuario, password = await UsuariosService(db).restablecer(usuario_id)
    await db.commit()
    await db.refresh(usuario)
    return CuentaConPasswordResponse(
        cuenta=CuentaResponse.model_validate(usuario), password_temporal=password
    )
