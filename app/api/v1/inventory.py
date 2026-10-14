"""Endpoints de inventario.

Todas las operaciones cuelgan de una funcion y se autorizan con el
client_id real del evento al que pertenece, cargado de la base, nunca con
un dato de la peticion.
"""

from fastapi import APIRouter, Depends, status

from app.api.deps import DB, UsuarioActual, requiere_rol, verificar_acceso_cliente
from app.core.errors import FUNCION_NO_EXISTE
from app.models.base import RolUsuario
from app.models.catalog import Performance
from app.schemas.inventory import (
    CambioDeEstadoRequest,
    CambioDeEstadoResponse,
    FuncionEstadoResponse,
    MaterializacionResponse,
    PrecioPorButacaRequest,
    ResumenResponse,
    SincronizacionResponse,
)
from app.services.catalog import CatalogService
from app.services.inventory import InventoryService

router = APIRouter(prefix="/performances", tags=["inventario"])

psd_u_organizador = Depends(requiere_rol(RolUsuario.ADMIN_PSD, RolUsuario.ORGANIZADOR))


async def _funcion_autorizada(
    performance_id: int, db: DB, usuario: UsuarioActual
) -> Performance:
    """Carga la funcion y valida acceso con el client_id de su evento."""
    servicio = InventoryService(db)
    funcion = await servicio.obtener_funcion(performance_id)
    evento = await CatalogService(db).obtener_evento(funcion.event_id)
    verificar_acceso_cliente(usuario, evento.client_id, no_encontrado=FUNCION_NO_EXISTE)
    return funcion


@router.get("/{performance_id}/inventario", response_model=ResumenResponse)
async def resumen_de_inventario(
    performance_id: int, db: DB, usuario: UsuarioActual
) -> ResumenResponse:
    await _funcion_autorizada(performance_id, db, usuario)
    resumen = await InventoryService(db).resumen(performance_id)
    return ResumenResponse(
        performance_id=resumen.performance_id,
        inventario_generado=resumen.inventario_generado,
        total=resumen.total,
        disponibles=resumen.disponibles,
        por_estado=resumen.por_estado,
    )


@router.post(
    "/{performance_id}/inventario",
    response_model=MaterializacionResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[psd_u_organizador],
)
async def materializar_inventario(
    performance_id: int, db: DB, usuario: UsuarioActual
) -> MaterializacionResponse:
    """Crea una butaca de inventario por cada butaca del mapa.

    Se corre una sola vez por funcion. Un segundo intento responde 409.
    """
    await _funcion_autorizada(performance_id, db, usuario)
    resultado = await InventoryService(db).materializar(performance_id)
    await db.commit()
    return MaterializacionResponse(**resultado.__dict__)


@router.post(
    "/{performance_id}/inventario/sincronizar",
    response_model=SincronizacionResponse,
    dependencies=[psd_u_organizador],
)
async def sincronizar_inventario(
    performance_id: int, db: DB, usuario: UsuarioActual
) -> SincronizacionResponse:
    """Agrega las butacas que el mapa gano despues de materializar."""
    await _funcion_autorizada(performance_id, db, usuario)
    agregadas = await InventoryService(db).sincronizar(performance_id)
    await db.commit()
    return SincronizacionResponse(
        performance_id=performance_id, butacas_agregadas=agregadas
    )


@router.post(
    "/{performance_id}/inventario/estado",
    response_model=CambioDeEstadoResponse,
    dependencies=[psd_u_organizador],
)
async def cambiar_estado_de_butacas(
    performance_id: int,
    datos: CambioDeEstadoRequest,
    db: DB,
    usuario: UsuarioActual,
) -> CambioDeEstadoResponse:
    """Bloquea, libera o marca butacas desde el panel, con bitacora."""
    await _funcion_autorizada(performance_id, db, usuario)
    afectadas = await InventoryService(db).cambiar_estado(
        performance_id,
        datos.performance_seat_ids,
        datos.estado,
        datos.motivo,
        actor_id=usuario.id,
    )
    await db.commit()
    return CambioDeEstadoResponse(butacas_afectadas=afectadas)


@router.put(
    "/{performance_id}/inventario/precios",
    response_model=CambioDeEstadoResponse,
    dependencies=[psd_u_organizador],
)
async def fijar_precio_por_butaca(
    performance_id: int,
    datos: PrecioPorButacaRequest,
    db: DB,
    usuario: UsuarioActual,
) -> CambioDeEstadoResponse:
    """Precio por excepcion. Sobrevive a cambios de precio de zona."""
    await _funcion_autorizada(performance_id, db, usuario)
    afectadas = await InventoryService(db).fijar_precio_butacas(
        performance_id, datos.performance_seat_ids, datos.precio
    )
    await db.commit()
    return CambioDeEstadoResponse(butacas_afectadas=afectadas)


@router.post(
    "/{performance_id}/inventario/aplicar-precios",
    response_model=CambioDeEstadoResponse,
    dependencies=[psd_u_organizador],
)
async def aplicar_precios_de_zona(
    performance_id: int, db: DB, usuario: UsuarioActual
) -> CambioDeEstadoResponse:
    """Baja los precios de zona vigentes al inventario ya materializado.

    Respeta las butacas con precio propio y las que ya se vendieron.
    """
    await _funcion_autorizada(performance_id, db, usuario)
    afectadas = await InventoryService(db).aplicar_precios_de_zona(performance_id)
    await db.commit()
    return CambioDeEstadoResponse(butacas_afectadas=afectadas)


@router.post(
    "/{performance_id}/abrir-venta",
    response_model=FuncionEstadoResponse,
    dependencies=[psd_u_organizador],
)
async def abrir_venta(
    performance_id: int, db: DB, usuario: UsuarioActual
) -> Performance:
    await _funcion_autorizada(performance_id, db, usuario)
    funcion = await InventoryService(db).abrir_venta(performance_id)
    await db.commit()
    await db.refresh(funcion)
    return funcion
