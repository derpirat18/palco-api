"""Cuentas del personal desde el panel.

PSD administra las de organizadores y de acceso de todos. Un organizador,
solo las de acceso de su propio organizador (U2): las crea, les da nueva
contraseña, las desactiva y las asigna a sus eventos. Las reglas viven en
app/api/deps.py; lo que no puede tocar le responde igual que lo inexistente.
"""

from fastapi import APIRouter, Depends, Query, status

from app.api.deps import (
    DB,
    UsuarioActual,
    alcance_cliente,
    organizador_de_cuenta_nueva,
    requiere_rol,
    verificar_cuenta_administrable,
)
from app.models.base import RolUsuario
from app.models.users import User
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
    dependencies=[Depends(requiere_rol(RolUsuario.ADMIN_PSD, RolUsuario.ORGANIZADOR))],
)


async def _cuenta(usuario_id: int, db: DB, usuario: User) -> User:
    cuenta = await UsuariosService(db).obtener(usuario_id)
    verificar_cuenta_administrable(usuario, cuenta)
    return cuenta


@router.get("", response_model=list[CuentaResponse])
async def listar_cuentas(
    db: DB, usuario: UsuarioActual, client_id: int | None = Query(default=None)
) -> list[CuentaResponse]:
    """PSD: todas (o las de un organizador). Organizador: su personal de acceso."""
    servicio = UsuariosService(db)
    if usuario.rol == RolUsuario.ADMIN_PSD:
        cuentas = await servicio.listar(client_id)
    else:
        cuentas = await servicio.listar(usuario.client_id, RolUsuario.ACCESO.value)
    return [CuentaResponse.model_validate(u) for u in cuentas]


@router.post(
    "",
    response_model=CuentaConPasswordResponse,
    status_code=status.HTTP_201_CREATED,
)
async def crear_cuenta(
    datos: CuentaCrear, db: DB, usuario: UsuarioActual
) -> CuentaConPasswordResponse:
    """La contrasena temporal sale solo en esta respuesta."""
    client_id = organizador_de_cuenta_nueva(usuario, datos.rol, datos.client_id)
    cuenta, password = await UsuariosService(db).crear(
        datos.email, datos.nombre, datos.rol, client_id
    )
    await db.commit()
    await db.refresh(cuenta)
    return CuentaConPasswordResponse(
        cuenta=CuentaResponse.model_validate(cuenta), password_temporal=password
    )


@router.patch("/{usuario_id}", response_model=CuentaResponse)
async def actualizar_cuenta(
    usuario_id: int, datos: CuentaActualizar, db: DB, usuario: UsuarioActual
) -> CuentaResponse:
    await _cuenta(usuario_id, db, usuario)
    cuenta = await UsuariosService(db).actualizar(
        usuario_id, datos.nombre, datos.activo
    )
    await db.commit()
    await db.refresh(cuenta)
    return CuentaResponse.model_validate(cuenta)


@router.post("/{usuario_id}/restablecer", response_model=CuentaConPasswordResponse)
async def restablecer_password(
    usuario_id: int, db: DB, usuario: UsuarioActual
) -> CuentaConPasswordResponse:
    """Para quien olvido su contrasena: una nueva temporal, que se muestra una vez."""
    await _cuenta(usuario_id, db, usuario)
    cuenta, password = await UsuariosService(db).restablecer(usuario_id)
    await db.commit()
    await db.refresh(cuenta)
    return CuentaConPasswordResponse(
        cuenta=CuentaResponse.model_validate(cuenta), password_temporal=password
    )


def _asignados(filas) -> list[EventoAsignadoResponse]:
    return [
        EventoAsignadoResponse(
            event_id=e.id, nombre=e.nombre, estado=e.estado, asignado_en=en
        )
        for e, en in filas
    ]


@router.get("/{usuario_id}/eventos", response_model=list[EventoAsignadoResponse])
async def eventos_asignados(
    usuario_id: int, db: DB, usuario: UsuarioActual
) -> list[EventoAsignadoResponse]:
    """Los eventos donde esta cuenta de acceso puede escanear."""
    await _cuenta(usuario_id, db, usuario)
    return _asignados(await AsignacionesService(db).de_cuenta(usuario_id))


@router.put("/{usuario_id}/eventos", response_model=list[EventoAsignadoResponse])
async def asignar_eventos(
    usuario_id: int, datos: AsignacionesRequest, db: DB, usuario: UsuarioActual
) -> list[EventoAsignadoResponse]:
    """Deja a la cuenta exactamente en estos eventos. Quitarle uno le corta
    el acceso a ese evento en la siguiente lectura. Una cuenta solo se asigna
    a eventos de su mismo organizador (lo revisa el servicio)."""
    await _cuenta(usuario_id, db, usuario)
    filas = await AsignacionesService(db).fijar(
        usuario_id, datos.event_ids, usuario.id, alcance=await alcance_cliente(usuario)
    )
    await db.commit()
    return _asignados(filas)
