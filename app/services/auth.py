"""Autenticacion del personal."""

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import (
    CredencialesInvalidas,
    hash_password,
    verificar_password,
)
from app.models.users import User


def normalizar_email(email: str) -> str:
    return email.strip().lower()


class AuthService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def autenticar(self, email: str, password: str) -> User:
        """Valida credenciales y devuelve el usuario.

        El mismo error para correo inexistente, contraseña incorrecta y
        cuenta inactiva: distinguirlos permitiria enumerar usuarios
        validos del sistema.
        """
        usuario = await self.db.scalar(
            select(User).where(User.email == normalizar_email(email))
        )

        if usuario is None:
            # Se verifica contra un hash falso para que el tiempo de
            # respuesta no delate si el correo existe.
            verificar_password(password, _HASH_SEÑUELO)
            raise CredencialesInvalidas("Correo o contraseña incorrectos.")

        if not verificar_password(password, usuario.password_hash):
            raise CredencialesInvalidas("Correo o contraseña incorrectos.")

        if not usuario.activo:
            raise CredencialesInvalidas("Correo o contraseña incorrectos.")

        usuario.ultimo_acceso = datetime.now(UTC)
        await self.db.commit()
        return usuario

    async def cambiar_password(
        self, usuario: User, password_actual: str, password_nueva: str
    ) -> None:
        if not verificar_password(password_actual, usuario.password_hash):
            raise CredencialesInvalidas("La contraseña actual no coincide.")
        usuario.password_hash = hash_password(password_nueva)
        await self.db.commit()


# Hash de una contraseña aleatoria, solo para igualar tiempos de respuesta.
_HASH_SEÑUELO = hash_password("señuelo-para-igualar-tiempos-de-respuesta")
