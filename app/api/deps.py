"""Dependencias compartidas de la API.

Toda la autorizacion pasa por aqui. Ningun endpoint verifica permisos a
mano: hay un solo lugar que auditar y un solo lugar que cambiar el dia
que los roles se vuelvan mas finos.

El aislamiento entre organizadores es la garantia critica de este
modulo: un organizador jamas puede leer ni tocar datos de otro cliente.
Esta cubierto por tests/test_auth_aislamiento.py.
"""

from collections.abc import Callable
from typing import Annotated

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.security import PermisoInsuficiente, TokenInvalido, decodificar_token
from app.models.base import RolUsuario
from app.models.users import User

# auto_error=False para que la ausencia de token caiga en nuestro formato
# de error y no en el genérico de FastAPI.
esquema_bearer = HTTPBearer(auto_error=False)

DB = Annotated[AsyncSession, Depends(get_db)]


async def get_current_user(
    db: DB,
    credenciales: Annotated[
        HTTPAuthorizationCredentials | None, Depends(esquema_bearer)
    ] = None,
) -> User:
    """Usuario del personal autenticado.

    Relee el usuario de la base en cada peticion. Es un viaje mas, pero
    permite revocar un acceso al instante: basta con poner activo=false.
    Con el rol del token nada mas, un usuario dado de baja seguiria
    entrando hasta que expirara su token.
    """
    if credenciales is None:
        raise TokenInvalido("Falta el token de acceso.")

    payload = decodificar_token(credenciales.credentials)

    try:
        user_id = int(payload["sub"])
    except (KeyError, ValueError) as exc:
        raise TokenInvalido("El token no identifica a un usuario.") from exc

    usuario = await db.scalar(select(User).where(User.id == user_id))

    if usuario is None or not usuario.activo:
        raise TokenInvalido("La cuenta no existe o esta inactiva.")

    return usuario


UsuarioActual = Annotated[User, Depends(get_current_user)]


def requiere_rol(*roles: RolUsuario) -> Callable:
    """Restringe un endpoint a ciertos roles.

    Uso:
        @router.post("/eventos", dependencies=[Depends(
            requiere_rol(RolUsuario.ADMIN_PSD, RolUsuario.ORGANIZADOR))])
    """
    permitidos = {r.value for r in roles}

    async def verificar(usuario: UsuarioActual) -> User:
        if usuario.rol not in permitidos:
            raise PermisoInsuficiente(
                "Tu rol no tiene acceso a esta operacion.",
                {"rol": usuario.rol, "requerido": sorted(permitidos)},
            )
        return usuario

    return verificar


async def alcance_cliente(usuario: UsuarioActual) -> int | None:
    """Organizador al que esta limitado el usuario.

    Devuelve None para admin_psd, que ve todos los organizadores. Las
    consultas filtran por este valor cuando no es None.
    """
    if usuario.rol == RolUsuario.ADMIN_PSD:
        return None
    return usuario.client_id


AlcanceCliente = Annotated[int | None, Depends(alcance_cliente)]


def verificar_acceso_cliente(usuario: User, client_id: int) -> None:
    """Valida que el usuario pueda tocar datos de ese organizador.

    Se llama en todo endpoint que reciba un identificador de recurso,
    DESPUES de cargarlo, con el client_id real del recurso. Confiar en un
    parametro de la peticion permitiria leer datos ajenos cambiando un
    numero en la URL.
    """
    if usuario.rol == RolUsuario.ADMIN_PSD:
        return

    if usuario.client_id != client_id:
        # Mismo mensaje que un 403 cualquiera: no revelamos si el
        # recurso existe o no, para no filtrar informacion por sondeo.
        raise PermisoInsuficiente("No tienes acceso a este recurso.")
