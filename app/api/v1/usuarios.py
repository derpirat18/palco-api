"""Cuentas del personal desde el panel. Solo PSD."""

from fastapi import APIRouter, Depends, Query, status

from app.api.deps import DB, UsuarioActual, requiere_rol
from app.models.base import RolUsuario
from app.schemas.usuarios import (
    AsignacionesRequest,
    CuentaActualizar,
    CuentaConPasswordResponse,
    CuentaCrear,
    CuentaResponse,
    EventoAsignadoResponse,
)
from app.services.asignaciones import AsignacionesService
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


def _asignados(filas) -> list[EventoAsignadoResponse]:
    return [
        EventoAsignadoResponse(
            event_id=e.id, nombre=e.nombre, estado=e.estado, asignado_en=en
        )
        for e, en in filas
    ]


@router.get("/{usuario_id}/eventos", response_model=list[EventoAsignadoResponse])
async def eventos_asignados(usuario_id: int, db: DB) -> list[EventoAsignadoResponse]:
    """Los eventos donde esta cuenta de acceso puede escanear."""
    return _asignados(await AsignacionesService(db).de_cuenta(usuario_id))


@router.put("/{usuario_id}/eventos", response_model=list[EventoAsignadoResponse])
async def asignar_eventos(
    usuario_id: int, datos: AsignacionesRequest, db: DB, actor: UsuarioActual
) -> list[EventoAsignadoResponse]:
    """Deja a la cuenta exactamente en estos eventos. Quitarle uno le corta
    el acceso a ese evento en la siguiente lectura."""
    filas = await AsignacionesService(db).fijar(usuario_id, datos.event_ids, actor.id)
    await db.commit()
    return _asignados(filas)
