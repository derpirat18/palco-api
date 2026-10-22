"""Control de acceso: lo que usa la puerta el dia de la funcion."""

from fastapi import APIRouter, Depends

from app.api.deps import DB, UsuarioActual, requiere_rol, verificar_acceso_cliente
from app.core.errors import FUNCION_NO_EXISTE
from app.models.base import RolUsuario
from app.schemas.acceso import ListaDeAccesoResponse
from app.services.catalog import CatalogService
from app.services.lista_de_acceso import ListaDeAccesoService

router = APIRouter(tags=["acceso"])

# El personal de acceso es de un organizador: ve solo sus funciones.
personal = Depends(
    requiere_rol(RolUsuario.ADMIN_PSD, RolUsuario.ORGANIZADOR, RolUsuario.ACCESO)
)


@router.get(
    "/performances/{performance_id}/lista-de-acceso",
    response_model=ListaDeAccesoResponse,
    dependencies=[personal],
)
async def lista_de_acceso(
    performance_id: int, db: DB, usuario: UsuarioActual
) -> ListaDeAccesoResponse:
    """Los boletos pagados de la funcion, ordenados por zona y asiento,
    para imprimir o descargar como respaldo del escaner."""
    servicio = CatalogService(db)
    funcion = await servicio.obtener_funcion(performance_id)
    evento = await servicio.obtener_evento(funcion.event_id)
    verificar_acceso_cliente(usuario, evento.client_id, no_encontrado=FUNCION_NO_EXISTE)
    lista = await ListaDeAccesoService(db).de_funcion(funcion, evento)
    return ListaDeAccesoResponse.model_validate(lista)
