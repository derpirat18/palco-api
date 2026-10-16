"""Organizadores: los clientes de PSD que venden boletos en la plataforma.

Las condiciones comerciales (comision, cargo por servicio, cargo por
orden) viven aqui, pero cada orden congela las suyas al momento de la
venta. Cambiar una comision afecta lo que se venda de hoy en adelante,
nunca el historico.
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ORGANIZADOR_NO_EXISTE, ErrorDeDominio, RecursoNoEncontrado
from app.models.catalog import Client, Event


class ConflictoDeOrganizador(ErrorDeDominio):
    codigo = "conflicto_de_organizador"
    http_status = 409


class ClientService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def listar(
        self, alcance: int | None, incluir_inactivos: bool = False
    ) -> list[Client]:
        """Lista los organizadores visibles para quien consulta.

        `alcance` es None para admin de PSD, que ve todos. Un organizador
        solo se ve a si mismo.
        """
        consulta = select(Client).order_by(Client.nombre)
        if alcance is not None:
            consulta = consulta.where(Client.id == alcance)
        if not incluir_inactivos:
            consulta = consulta.where(Client.activo.is_(True))
        return list((await self.db.scalars(consulta)).all())

    async def obtener(self, client_id: int) -> Client:
        cliente = await self.db.get(Client, client_id)
        if cliente is None:
            raise RecursoNoEncontrado(ORGANIZADOR_NO_EXISTE)
        return cliente

    async def crear(self, datos: dict) -> Client:
        """Alta de organizador.

        El nombre no lleva restriccion unica en la base: la identidad real
        es el RFC, que al dar de alta no siempre se tiene. Aqui se avisa
        del duplicado para evitar el alta por descuido. No es a prueba de
        carreras y no necesita serlo: las da de alta PSD a mano, de una en
        una, unas cuantas veces al año.
        """
        nombre = datos["nombre"].strip()
        repetido = await self.db.scalar(
            select(Client).where(func.lower(Client.nombre) == nombre.lower())
        )
        if repetido is not None:
            raise ConflictoDeOrganizador(
                f"Ya existe un organizador llamado '{repetido.nombre}'.",
                {"client_id": repetido.id},
            )

        cliente = Client(**{**datos, "nombre": nombre})
        self.db.add(cliente)
        await self.db.flush()
        return cliente

    async def actualizar(self, client_id: int, cambios: dict) -> Client:
        cliente = await self.obtener(client_id)

        nombre = cambios.get("nombre")
        if nombre and nombre.strip().lower() != cliente.nombre.lower():
            repetido = await self.db.scalar(
                select(Client).where(
                    func.lower(Client.nombre) == nombre.strip().lower(),
                    Client.id != client_id,
                )
            )
            if repetido is not None:
                raise ConflictoDeOrganizador(
                    f"Ya existe un organizador llamado '{repetido.nombre}'.",
                    {"client_id": repetido.id},
                )

        for campo, valor in cambios.items():
            setattr(cliente, campo, valor)

        await self.db.flush()
        return cliente

    async def desactivar(self, client_id: int) -> Client:
        """Baja logica. No se borra: su historico de ventas lo referencia.

        Con eventos publicados no se permite: dejaria a la venta funciones
        de un organizador dado de baja.
        """
        cliente = await self.obtener(client_id)

        publicados = await self.db.scalar(
            select(func.count(Event.id)).where(
                Event.client_id == client_id, Event.estado == "publicado"
            )
        )
        if publicados:
            raise ConflictoDeOrganizador(
                "El organizador tiene eventos publicados. Archívalos primero.",
                {"eventos_publicados": int(publicados)},
            )

        cliente.activo = False
        await self.db.flush()
        return cliente
