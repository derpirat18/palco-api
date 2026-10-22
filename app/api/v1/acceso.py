"""Control de acceso: lo que usa la puerta el dia de la funcion.

Y el listado de ventas, que no es de la puerta pero cuelga de la misma
funcion: lo ven PSD y el organizador, nunca el personal de acceso.
"""

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Response
from sqlalchemy import select

from app.api.deps import (
    DB,
    UsuarioActual,
    requiere_rol,
    verificar_acceso_cliente,
    verificar_acceso_evento,
)
from app.core.errors import FUNCION_NO_EXISTE
from app.models.acceso import AsignacionDeAcceso
from app.models.base import EstadoFuncion, RolUsuario
from app.models.catalog import Event, Performance, Venue
from app.schemas.acceso import (
    EscaneoRequest,
    EscaneoResponse,
    FuncionDePuertaResponse,
    ListaDeAccesoResponse,
    ListadoDeVentasResponse,
)
from app.services.catalog import CatalogService
from app.services.escaner import EscanerService
from app.services.lista_de_acceso import ListaDeAccesoService
from app.services.ventas import VentasService, a_excel

router = APIRouter(tags=["acceso"])

# El personal de acceso es de un organizador y ve solo los eventos a los
# que lo asignaron (deps.verificar_acceso_evento).
personal = Depends(
    requiere_rol(RolUsuario.ADMIN_PSD, RolUsuario.ORGANIZADOR, RolUsuario.ACCESO)
)
psd_u_organizador = Depends(requiere_rol(RolUsuario.ADMIN_PSD, RolUsuario.ORGANIZADOR))

# Una funcion sigue en la lista del escaner hasta medio dia despues de
# empezar: hay funciones largas y gente que llega tarde.
_VIGENCIA = timedelta(hours=12)


async def _funcion_de_la_puerta(performance_id: int, db: DB, usuario: UsuarioActual):
    servicio = CatalogService(db)
    funcion = await servicio.obtener_funcion(performance_id)
    evento = await servicio.obtener_evento(funcion.event_id)
    await verificar_acceso_evento(
        db, usuario, evento.id, evento.client_id, no_encontrado=FUNCION_NO_EXISTE
    )
    return funcion, evento


@router.get(
    "/acceso/funciones",
    response_model=list[FuncionDePuertaResponse],
    dependencies=[personal],
)
async def funciones_de_la_puerta(
    db: DB, usuario: UsuarioActual
) -> list[FuncionDePuertaResponse]:
    """Las funciones de hoy en adelante donde esta cuenta puede escanear."""
    consulta = (
        select(Performance, Event, Venue)
        .join(Event, Event.id == Performance.event_id)
        .join(Venue, Venue.id == Event.venue_id)
        .where(
            Performance.inicia_en >= datetime.now(UTC) - _VIGENCIA,
            Performance.estado != EstadoFuncion.CANCELADA.value,
        )
        .order_by(Performance.inicia_en)
        .limit(60)
    )
    if usuario.rol != RolUsuario.ADMIN_PSD:
        consulta = consulta.where(Event.client_id == usuario.client_id)
    if usuario.rol == RolUsuario.ACCESO:
        consulta = consulta.where(
            Event.id.in_(
                select(AsignacionDeAcceso.event_id).where(
                    AsignacionDeAcceso.user_id == usuario.id
                )
            )
        )
    filas = (await db.execute(consulta)).all()
    contadores = await EscanerService(db).contadores([f.id for f, _, _ in filas])
    return [
        FuncionDePuertaResponse(
            performance_id=funcion.id,
            event_id=evento.id,
            evento=evento.nombre,
            recinto=recinto.nombre,
            ciudad=recinto.ciudad,
            zona_horaria=recinto.zona_horaria,
            inicia_en=funcion.inicia_en,
            apertura_puertas=funcion.apertura_puertas,
            estado=funcion.estado,
            total=contadores[funcion.id].total,
            usados=contadores[funcion.id].usados,
        )
        for funcion, evento, recinto in filas
    ]


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
    funcion, evento = await _funcion_de_la_puerta(performance_id, db, usuario)
    lista = await ListaDeAccesoService(db).de_funcion(funcion, evento)
    return ListaDeAccesoResponse.model_validate(lista)


@router.post(
    "/performances/{performance_id}/escanear",
    response_model=EscaneoResponse,
    dependencies=[personal],
)
async def escanear(
    performance_id: int, datos: EscaneoRequest, db: DB, usuario: UsuarioActual
) -> EscaneoResponse:
    """Lee un boleto en la puerta: si entra, queda como usado en ese momento.

    Siempre responde 200 con el resultado (valido, duplicado, otra_funcion,
    invalido, cancelado): un boleto que no entra no es un error del
    sistema, es lo que la puerta tiene que ver.
    """
    funcion, _ = await _funcion_de_la_puerta(performance_id, db, usuario)
    escaneo = await EscanerService(db).escanear(
        funcion, datos.codigo, datos.client_scan_id, usuario
    )
    await db.commit()
    return EscaneoResponse.model_validate(escaneo)


async def _funcion_del_organizador(performance_id: int, db: DB, usuario: UsuarioActual):
    servicio = CatalogService(db)
    funcion = await servicio.obtener_funcion(performance_id)
    evento = await servicio.obtener_evento(funcion.event_id)
    verificar_acceso_cliente(usuario, evento.client_id, no_encontrado=FUNCION_NO_EXISTE)
    return funcion, evento


@router.get(
    "/performances/{performance_id}/ventas",
    response_model=ListadoDeVentasResponse,
    dependencies=[psd_u_organizador],
)
async def ventas_de_funcion(
    performance_id: int, db: DB, usuario: UsuarioActual
) -> ListadoDeVentasResponse:
    """Cada butaca vendida o regalada, con los datos del comprador.

    Lleva correo y telefono: la consulta queda en la bitacora."""
    funcion, evento = await _funcion_del_organizador(performance_id, db, usuario)
    servicio = VentasService(db)
    listado = await servicio.de_funcion(funcion, evento)
    await servicio.registrar(usuario.id, funcion.id, "ver")
    await db.commit()
    return ListadoDeVentasResponse.model_validate(listado)


@router.get(
    "/performances/{performance_id}/ventas.xlsx",
    dependencies=[psd_u_organizador],
    response_class=Response,
    responses={
        200: {
            "content": {
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": {}
            }
        }
    },
)
async def descargar_ventas(
    performance_id: int, db: DB, usuario: UsuarioActual
) -> Response:
    """El mismo listado en Excel. Descargar queda en la bitacora aparte de ver:
    los datos salen del sistema."""
    funcion, evento = await _funcion_del_organizador(performance_id, db, usuario)
    servicio = VentasService(db)
    listado = await servicio.de_funcion(funcion, evento)
    contenido = a_excel(listado)
    await servicio.registrar(usuario.id, funcion.id, "descargar")
    await db.commit()
    return Response(
        content=contenido,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": (
                f'attachment; filename="ventas-funcion-{funcion.id}.xlsx"'
            ),
            "Cache-Control": "no-store",
        },
    )
