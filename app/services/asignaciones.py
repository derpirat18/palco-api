"""A que eventos entra cada cuenta de acceso (E3a).

Por ahora las asigna PSD. Una cuenta de acceso solo puede asignarse a
eventos de su mismo organizador: es su personal.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import RecursoNoEncontrado
from app.models.acceso import AsignacionDeAcceso
from app.models.base import RolUsuario
from app.models.catalog import Event
from app.services.usuarios import ConflictoDeCuenta, UsuariosService


class AsignacionesService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def de_cuenta(self, usuario_id: int) -> list[tuple[Event, datetime]]:
        await self._cuenta_de_acceso(usuario_id)
        return [
            (evento, en)
            for evento, en in (
                await self.db.execute(
                    select(Event, AsignacionDeAcceso.asignado_en)
                    .join(AsignacionDeAcceso, AsignacionDeAcceso.event_id == Event.id)
                    .where(AsignacionDeAcceso.user_id == usuario_id)
                    .order_by(Event.nombre)
                )
            ).all()
        ]

    async def fijar(
        self, usuario_id: int, event_ids: list[int], actor_id: int
    ) -> list[tuple[Event, datetime]]:
        """Deja a la cuenta exactamente en estos eventos."""
        usuario = await self._cuenta_de_acceso(usuario_id)
        nuevos = set(event_ids)
        eventos = {
            e.id: e
            for e in (
                await self.db.scalars(select(Event).where(Event.id.in_(nuevos)))
            ).all()
        }
        if faltan := nuevos - eventos.keys():
            raise RecursoNoEncontrado(
                "Uno de los eventos no existe.", {"event_ids": sorted(faltan)}
            )
        if ajenos := [
            e.nombre for e in eventos.values() if e.client_id != usuario.client_id
        ]:
            raise ConflictoDeCuenta(
                "Esta cuenta es de otro organizador: no puede escanear en "
                + ", ".join(sorted(ajenos))
                + ".",
                {"eventos": sorted(ajenos)},
            )

        actuales = set(
            (
                await self.db.scalars(
                    select(AsignacionDeAcceso.event_id).where(
                        AsignacionDeAcceso.user_id == usuario_id
                    )
                )
            ).all()
        )
        if quitar := actuales - nuevos:
            await self.db.execute(
                delete(AsignacionDeAcceso).where(
                    AsignacionDeAcceso.user_id == usuario_id,
                    AsignacionDeAcceso.event_id.in_(quitar),
                )
            )
        self.db.add_all(
            AsignacionDeAcceso(user_id=usuario_id, event_id=eid, asignado_por=actor_id)
            for eid in sorted(nuevos - actuales)
        )
        await self.db.flush()
        return await self.de_cuenta(usuario_id)

    async def _cuenta_de_acceso(self, usuario_id: int):
        usuario = await UsuariosService(self.db).obtener(usuario_id)
        if usuario.rol != RolUsuario.ACCESO.value:
            raise ConflictoDeCuenta(
                "Solo las cuentas de control de acceso se asignan a eventos. "
                "Un organizador ya ve todos sus eventos."
            )
        return usuario
