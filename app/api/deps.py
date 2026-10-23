"""Dependencias compartidas de la API.

Toda la autorizacion pasa por aqui. Ningun endpoint verifica permisos a
mano: hay un solo lugar que auditar y un solo lugar que cambiar el dia
que los roles se vuelvan mas finos.

El aislamiento entre organizadores es la garantia critica de este
modulo: un organizador jamas puede leer ni tocar datos de otro cliente.
Esta cubierto por tests/test_auth.py y por las pruebas de cada router
que comparan la respuesta a un recurso ajeno con la de uno inexistente.
"""

from collections.abc import Callable
from typing import Annotated

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.errors import CUENTA_NO_EXISTE, RecursoNoEncontrado
from app.core.security import PermisoInsuficiente, TokenInvalido, decodificar_token
from app.models.acceso import AsignacionDeAcceso
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
        raise TokenInvalido("Inicia sesión para continuar.")

    payload = decodificar_token(credenciales.credentials)

    try:
        user_id = int(payload["sub"])
    except (KeyError, ValueError) as exc:
        raise TokenInvalido("Tu sesión terminó. Vuelve a iniciar sesión.") from exc

    usuario = await db.scalar(select(User).where(User.id == user_id))

    if usuario is None or not usuario.activo:
        raise TokenInvalido(
            "Tu cuenta no está activa. Si crees que es un error, escríbenos."
        )

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
                "Tu cuenta no tiene permiso para esta acción.",
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


def verificar_acceso_cliente(
    usuario: User, client_id: int, *, no_encontrado: str
) -> None:
    """Valida que el usuario pueda tocar datos de ese organizador.

    Se llama en todo endpoint que reciba un identificador de recurso,
    DESPUES de cargarlo, con el client_id real del recurso. Confiar en un
    parametro de la peticion permitiria leer datos ajenos cambiando un
    numero en la URL.

    Lo ajeno se responde exactamente igual que lo inexistente: mismo 404 y
    mismo mensaje. Con un 403 para lo ajeno, un organizador podria
    recorrer identificadores y saber cuales existen y de quien son. Por eso
    `no_encontrado` es obligatorio y tiene que ser el mismo texto que da el
    servicio cuando ese recurso no existe.
    """
    if usuario.rol == RolUsuario.ADMIN_PSD:
        return

    if usuario.client_id != client_id:
        raise RecursoNoEncontrado(no_encontrado)


async def verificar_acceso_evento(
    db: AsyncSession,
    usuario: User,
    event_id: int,
    client_id: int,
    *,
    no_encontrado: str,
) -> None:
    """Como `verificar_acceso_cliente`, y ademas la asignacion del personal de acceso.

    Una cuenta de acceso es de un organizador, pero solo entra a los
    eventos a los que PSD la asigno (E3a): quien escanea en Puebla no abre
    la lista de Tuxtla. Para ella, un evento no asignado no existe: el
    mismo 404 que uno inexistente.
    """
    verificar_acceso_cliente(usuario, client_id, no_encontrado=no_encontrado)
    if usuario.rol != RolUsuario.ACCESO:
        return
    asignado = await db.scalar(
        select(AsignacionDeAcceso.id).where(
            AsignacionDeAcceso.user_id == usuario.id,
            AsignacionDeAcceso.event_id == event_id,
        )
    )
    if asignado is None:
        raise RecursoNoEncontrado(no_encontrado)


def verificar_cuenta_administrable(usuario: User, cuenta: User) -> None:
    """Quien puede tocar una cuenta del personal desde el panel (U2).

    PSD, cualquiera de organizador o de acceso. Un organizador, solo las de
    acceso de su propio organizador: ni las de otro organizador ni las de
    sus compañeros organizadores. Lo que no puede tocar no existe para él.
    """
    if usuario.rol == RolUsuario.ADMIN_PSD:
        return
    if cuenta.rol != RolUsuario.ACCESO or cuenta.client_id != usuario.client_id:
        raise RecursoNoEncontrado(CUENTA_NO_EXISTE)


def organizador_de_cuenta_nueva(
    usuario: User, rol: str, client_id: int | None
) -> int | None:
    """El organizador de una cuenta que se crea desde el panel.

    PSD elige el organizador (y si falta, el servicio lo dice). Un
    organizador solo crea personal de acceso, y siempre para sí mismo: su
    client_id lo pone el servidor, nunca la petición.
    """
    if usuario.rol == RolUsuario.ADMIN_PSD:
        return client_id
    if rol != RolUsuario.ACCESO:
        raise PermisoInsuficiente(
            "Como organizador solo puedes crear cuentas de control de acceso.",
            {"rol": rol},
        )
    return usuario.client_id
