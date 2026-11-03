"""Metricas del panel (bloque 14): solo lectura. Reglas en app/services/metricas.py.

PSD ve todo; el organizador solo lo suyo (lo ajeno responde igual que lo que
no existe); el personal de acceso no tiene estas pantallas.
"""

from fastapi import APIRouter
from sqlalchemy import select

from app.api.deps import DB, UsuarioActual, verificar_acceso_cliente
from app.core.errors import EVENTO_NO_EXISTE, FUNCION_NO_EXISTE, RecursoNoEncontrado
from app.core.security import PermisoInsuficiente
from app.models.base import RolUsuario
from app.models.catalog import Event, Performance, Venue
from app.models.users import User
from app.services import metricas as svc

router = APIRouter(prefix="/panel/metricas", tags=["metricas"])


def _de_quien(usuario: User) -> int | None:
    if usuario.rol == RolUsuario.ADMIN_PSD:
        return None
    if usuario.rol == RolUsuario.ORGANIZADOR:
        return usuario.client_id
    raise PermisoInsuficiente("Tu cuenta no tiene acceso a esta pantalla.")


@router.get("/eventos")
async def eventos(db: DB, usuario: UsuarioActual) -> dict:
    return {"eventos": await svc.eventos(db, _de_quien(usuario))}


@router.get("/eventos/{event_id}/funciones")
async def funciones(event_id: int, db: DB, usuario: UsuarioActual) -> dict:
    _de_quien(usuario)
    fila = (
        await db.execute(
            select(Event, Venue)
            .join(Venue, Venue.id == Event.venue_id)
            .where(Event.id == event_id)
        )
    ).first()
    if fila is None:
        raise RecursoNoEncontrado(EVENTO_NO_EXISTE)
    evento, venue = fila
    verificar_acceso_cliente(usuario, evento.client_id, no_encontrado=EVENTO_NO_EXISTE)
    return {"funciones": await svc.funciones_de_evento(db, evento, venue)}


@router.get("/funciones/{performance_id}")
async def funcion(performance_id: int, db: DB, usuario: UsuarioActual) -> dict:
    _de_quien(usuario)
    fila = (
        await db.execute(
            select(Performance, Event, Venue)
            .join(Event, Event.id == Performance.event_id)
            .join(Venue, Venue.id == Event.venue_id)
            .where(Performance.id == performance_id)
        )
    ).first()
    if fila is None:
        raise RecursoNoEncontrado(FUNCION_NO_EXISTE)
    f, evento, venue = fila
    verificar_acceso_cliente(usuario, evento.client_id, no_encontrado=FUNCION_NO_EXISTE)
    return await svc.funcion(db, f, evento, venue)
