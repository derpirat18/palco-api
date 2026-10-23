"""Cuentas del personal que se dan de alta desde el panel.

PSD da de alta organizadores y personal de acceso; un organizador, solo
su propio personal de acceso (U2, permisos en app/api/deps.py). Las
cuentas de admin de PSD se siguen creando con `scripts/crear_usuario.py`:
quien entra al panel no puede repartir acceso total.

La contrasena inicial la genera el sistema y se muestra una sola vez a
quien crea la cuenta, para que se la pase a la persona (no hay correo
de invitacion mientras SES siga en modo de pruebas). Nunca se guarda
en claro ni vuelve a salir en ninguna respuesta.
"""

from __future__ import annotations

import secrets

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import CUENTA_NO_EXISTE, ErrorDeDominio, RecursoNoEncontrado
from app.core.security import hash_password
from app.models.base import RolUsuario
from app.models.catalog import Client
from app.models.users import User
from app.services.auth import normalizar_email

ROLES_DEL_PANEL = {RolUsuario.ORGANIZADOR.value, RolUsuario.ACCESO.value}

# Solo minusculas y numeros, sin los que se confunden (0/o, 1/l/i): con
# mayusculas, letras como c/C, s/S o x/X se ven casi iguales y una persona
# que la copia a mano o la dicta por telefono no entra.
_ALFABETO = "abcdefghjkmnpqrstuvwxyz23456789"


class ConflictoDeCuenta(ErrorDeDominio):
    codigo = "conflicto_de_cuenta"
    http_status = 409


def password_temporal() -> str:
    """16 caracteres en cuatro grupos de 4: facil de dictar, ~79 bits de azar."""
    c = "".join(secrets.choice(_ALFABETO) for _ in range(16))
    return "-".join(c[i : i + 4] for i in range(0, 16, 4))


class UsuariosService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def listar(
        self, client_id: int | None = None, rol: str | None = None
    ) -> list[User]:
        consulta = select(User).order_by(User.rol, User.nombre)
        if client_id is not None:
            consulta = consulta.where(User.client_id == client_id)
        if rol is not None:
            consulta = consulta.where(User.rol == rol)
        return list((await self.db.scalars(consulta)).all())

    async def crear(
        self, email: str, nombre: str, rol: str, client_id: int | None
    ) -> tuple[User, str]:
        if rol not in ROLES_DEL_PANEL:
            raise ConflictoDeCuenta(
                "Desde el panel solo se crean cuentas de organizador y de "
                "control de acceso."
            )
        if client_id is None:
            raise ConflictoDeCuenta("Elige el organizador de la cuenta.")
        if await self.db.get(Client, client_id) is None:
            raise RecursoNoEncontrado("El organizador no existe.")
        email = normalizar_email(email)
        if await self.db.scalar(select(User.id).where(User.email == email)):
            raise ConflictoDeCuenta("Ya existe una cuenta con ese correo.")

        password = password_temporal()
        usuario = User(
            email=email,
            nombre=nombre.strip(),
            password_hash=hash_password(password),
            rol=rol,
            client_id=client_id,
        )
        self.db.add(usuario)
        await self.db.flush()
        return usuario, password

    async def obtener(self, usuario_id: int) -> User:
        usuario = await self.db.get(User, usuario_id)
        # Las de admin de PSD no se tocan desde el panel: para el panel no existen.
        if usuario is None or usuario.rol not in ROLES_DEL_PANEL:
            raise RecursoNoEncontrado(CUENTA_NO_EXISTE)
        return usuario

    async def actualizar(
        self, usuario_id: int, nombre: str | None, activo: bool | None
    ) -> User:
        usuario = await self.obtener(usuario_id)
        if nombre is not None:
            usuario.nombre = nombre.strip()
        if activo is not None:
            # Desactivar corta el acceso en la siguiente peticion: el usuario
            # se relee de la base en cada una (app/api/deps.py).
            usuario.activo = activo
        await self.db.flush()
        return usuario

    async def restablecer(self, usuario_id: int) -> tuple[User, str]:
        usuario = await self.obtener(usuario_id)
        password = password_temporal()
        usuario.password_hash = hash_password(password)
        await self.db.flush()
        return usuario, password
