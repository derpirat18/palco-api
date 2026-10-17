"""Cortesias (C1): las da el organizador sobre su propia funcion.

Se autorizan con el client_id real del evento, cargado de la base, nunca
con un dato de la peticion. Lo ajeno responde igual que lo inexistente.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, status
from sqlalchemy import select

from app.api.deps import DB, UsuarioActual, requiere_rol, verificar_acceso_cliente
from app.core.errors import FUNCION_NO_EXISTE
from app.models.base import RolUsuario
from app.models.catalog import Event, Performance
from app.models.sales import Cortesia, Customer, Order, OrderItem
from app.schemas.cortesias import (
    CortesiaRequest,
    CortesiaResponse,
    LugarDeCortesiaResponse,
)
from app.services.catalog import CatalogService
from app.services.cortesias import CORTESIA_NO_EXISTE, CortesiaService
from app.services.inventory import InventoryService

router = APIRouter(tags=["cortesias"])

psd_u_organizador = Depends(requiere_rol(RolUsuario.ADMIN_PSD, RolUsuario.ORGANIZADOR))


async def _autorizar_funcion(
    performance_id: int, db: DB, usuario: UsuarioActual, no_encontrado: str
) -> Performance:
    funcion = await InventoryService(db).obtener_funcion(performance_id)
    evento = await CatalogService(db).obtener_evento(funcion.event_id)
    verificar_acceso_cliente(usuario, evento.client_id, no_encontrado=no_encontrado)
    return funcion


def _respuesta(
    orden: Order, cortesia: Cortesia, persona: Customer, partidas: list[OrderItem]
) -> CortesiaResponse:
    return CortesiaResponse(
        uuid=orden.uuid,
        folio=orden.folio,
        estado=orden.estado,
        email=persona.email,
        nombre=persona.nombre,
        motivo=cortesia.motivo,
        otorgada_en=cortesia.otorgada_en,
        revocada_en=cortesia.revocada_en,
        lugares=[
            LugarDeCortesiaResponse(zona=p.zona_snap, asiento=p.asiento_snap)
            for p in partidas
        ],
    )


@router.post(
    "/performances/{performance_id}/cortesias",
    response_model=CortesiaResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[psd_u_organizador],
)
async def otorgar(
    performance_id: int, datos: CortesiaRequest, db: DB, usuario: UsuarioActual
) -> CortesiaResponse:
    """Da boletos de cortesia: consumen inventario y llegan por correo con QR."""
    await _autorizar_funcion(performance_id, db, usuario, FUNCION_NO_EXISTE)
    emitida = await CortesiaService(db).otorgar(
        performance_id,
        datos.seat_ids,
        str(datos.email),
        datos.nombre,
        datos.motivo,
        actor_id=usuario.id,
    )
    await db.commit()
    return _respuesta(
        emitida.orden, emitida.cortesia, emitida.comprador, emitida.partidas
    )


@router.get(
    "/performances/{performance_id}/cortesias",
    response_model=list[CortesiaResponse],
    dependencies=[psd_u_organizador],
)
async def listar(
    performance_id: int, db: DB, usuario: UsuarioActual
) -> list[CortesiaResponse]:
    await _autorizar_funcion(performance_id, db, usuario, FUNCION_NO_EXISTE)
    return [
        _respuesta(*fila) for fila in await CortesiaService(db).listar(performance_id)
    ]


@router.post(
    "/cortesias/{orden_uuid}/revocar",
    response_model=CortesiaResponse,
    dependencies=[psd_u_organizador],
)
async def revocar(orden_uuid: UUID, db: DB, usuario: UsuarioActual) -> CortesiaResponse:
    """Cancela los boletos y devuelve los lugares. Repetirlo no hace nada."""
    servicio = CortesiaService(db)
    orden, _ = await servicio.obtener(orden_uuid)
    funcion = await db.get(Performance, orden.performance_id)
    evento = await db.get(Event, funcion.event_id)
    verificar_acceso_cliente(
        usuario, evento.client_id, no_encontrado=CORTESIA_NO_EXISTE
    )
    orden = await servicio.revocar(orden_uuid, actor_id=usuario.id)
    await db.commit()
    orden, cortesia = await servicio.obtener(orden_uuid)
    persona = await db.get(Customer, orden.customer_id)
    partidas = list(
        (
            await db.scalars(
                select(OrderItem)
                .where(OrderItem.order_id == orden.id)
                .order_by(OrderItem.id)
            )
        ).all()
    )
    return _respuesta(orden, cortesia, persona, partidas)
