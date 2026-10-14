"""Endpoints de organizadores.

Quien edita que es la decision importante de este modulo: las
condiciones comerciales son el acuerdo entre PSD y el organizador, asi
que solo PSD las toca. El organizador puede leer las suyas y mantener
sus datos de contacto, nunca su comision.
"""

from fastapi import APIRouter, Depends, Query, status

from app.api.deps import (
    DB,
    AlcanceCliente,
    UsuarioActual,
    requiere_rol,
    verificar_acceso_cliente,
)
from app.core.errors import ORGANIZADOR_NO_EXISTE
from app.core.security import PermisoInsuficiente
from app.models.base import RolUsuario
from app.schemas.clients import ClienteActualizar, ClienteCrear, ClienteResponse
from app.services.clients import ClientService

router = APIRouter(prefix="/clients", tags=["organizadores"])

solo_psd = Depends(requiere_rol(RolUsuario.ADMIN_PSD))

# Campos que un organizador puede cambiar de si mismo. Todo lo demas
# (comision, cargos, activo) es acuerdo comercial y lo mueve PSD.
CAMPOS_DEL_ORGANIZADOR = frozenset(
    {
        "razon_social",
        "rfc",
        "direccion",
        "contacto",
        "telefono",
        "email",
        "clabe",
        "ga_id",
        "pixel_id",
    }
)


@router.get("", response_model=list[ClienteResponse])
async def listar_organizadores(
    db: DB,
    alcance: AlcanceCliente,
    incluir_inactivos: bool = Query(default=False),
) -> list:
    return await ClientService(db).listar(alcance, incluir_inactivos)


@router.post(
    "",
    response_model=ClienteResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[solo_psd],
)
async def crear_organizador(datos: ClienteCrear, db: DB):
    # exclude_none para que las condiciones comerciales no indicadas caigan
    # en los valores por omision del modelo en lugar de intentar un NULL.
    cliente = await ClientService(db).crear(datos.model_dump(exclude_none=True))
    await db.commit()
    await db.refresh(cliente)
    return cliente


@router.get("/{client_id}", response_model=ClienteResponse)
async def obtener_organizador(client_id: int, db: DB, usuario: UsuarioActual):
    cliente = await ClientService(db).obtener(client_id)
    verificar_acceso_cliente(usuario, cliente.id, no_encontrado=ORGANIZADOR_NO_EXISTE)
    return cliente


@router.patch("/{client_id}", response_model=ClienteResponse)
async def actualizar_organizador(
    client_id: int, datos: ClienteActualizar, db: DB, usuario: UsuarioActual
):
    servicio = ClientService(db)
    cliente = await servicio.obtener(client_id)
    verificar_acceso_cliente(usuario, cliente.id, no_encontrado=ORGANIZADOR_NO_EXISTE)

    cambios = datos.model_dump(exclude_unset=True)

    if usuario.rol != RolUsuario.ADMIN_PSD:
        prohibidos = sorted(set(cambios) - CAMPOS_DEL_ORGANIZADOR)
        if prohibidos:
            raise PermisoInsuficiente(
                "Esos campos los administra PSD.",
                {"campos": prohibidos},
            )

    cliente = await servicio.actualizar(client_id, cambios)
    await db.commit()
    await db.refresh(cliente)
    return cliente


@router.delete(
    "/{client_id}",
    response_model=ClienteResponse,
    dependencies=[solo_psd],
)
async def desactivar_organizador(client_id: int, db: DB):
    """Baja logica: el historico de ventas sigue apuntando a este registro."""
    cliente = await ClientService(db).desactivar(client_id)
    await db.commit()
    await db.refresh(cliente)
    return cliente
