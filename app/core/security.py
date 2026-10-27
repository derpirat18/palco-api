"""Hash de contraseñas y tokens de acceso.

Se usa bcrypt directamente en lugar de passlib: una capa menos y sin los
problemas de compatibilidad conocidos entre passlib y bcrypt 4+.
"""

from datetime import UTC, datetime, timedelta
from typing import Any

import bcrypt
from jose import JWTError, jwt

from app.core.config import settings
from app.core.errors import ErrorDeDominio

ALGORITMO = "HS256"

# bcrypt ignora en silencio todo lo que pase de 72 bytes. Truncar sin
# avisar es una trampa de seguridad: dos contraseñas distintas que
# comparten los primeros 72 bytes serian equivalentes. Mejor rechazar.
MAX_BYTES_PASSWORD = 72


class CredencialesInvalidas(ErrorDeDominio):
    codigo = "credenciales_invalidas"
    http_status = 401


class TokenInvalido(ErrorDeDominio):
    codigo = "token_invalido"
    http_status = 401


class PermisoInsuficiente(ErrorDeDominio):
    codigo = "permiso_insuficiente"
    http_status = 403


class PasswordDemasiadoLarga(ErrorDeDominio):
    codigo = "password_demasiado_larga"
    http_status = 422


def hash_password(password: str) -> str:
    datos = password.encode("utf-8")
    if len(datos) > MAX_BYTES_PASSWORD:
        raise PasswordDemasiadoLarga(
            f"La contraseña no puede exceder {MAX_BYTES_PASSWORD} bytes."
        )
    return bcrypt.hashpw(datos, bcrypt.gensalt()).decode("utf-8")


def verificar_password(password: str, hash_guardado: str) -> bool:
    datos = password.encode("utf-8")
    if len(datos) > MAX_BYTES_PASSWORD:
        return False
    try:
        return bcrypt.checkpw(datos, hash_guardado.encode("utf-8"))
    except (ValueError, TypeError):
        # Hash corrupto o de otro algoritmo: no es credencial valida.
        return False


def crear_access_token(
    subject: str | int,
    rol: str,
    client_id: int | None = None,
    expira_en: timedelta | None = None,
) -> str:
    """Firma un token de acceso.

    El rol y el client_id viajan dentro del token para no pegarle a la
    base en cada peticion. El costo es que un cambio de rol no surte
    efecto hasta que expire el token: por eso la vigencia es corta.
    """
    ahora = datetime.now(UTC)
    expira = ahora + (
        expira_en or timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    )
    payload: dict[str, Any] = {
        "sub": str(subject),
        "rol": rol,
        "client_id": client_id,
        "iat": ahora,
        "exp": expira,
        "tipo": "access",
    }
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=ALGORITMO)


def decodificar_token(token: str) -> dict[str, Any]:
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[ALGORITMO])
    except JWTError as exc:
        raise TokenInvalido("Tu sesión terminó. Vuelve a iniciar sesión.") from exc

    if payload.get("tipo") != "access":
        raise TokenInvalido("Tu sesión terminó. Vuelve a iniciar sesión.")

    return payload


# --------------------------------------------------------------------------
# Comprador (R1)
# --------------------------------------------------------------------------

# Un comprador entra con un codigo a su correo y se queda adentro en ese
# equipo. Si quiere salir de todos, se sube su `sesion_version`.
SESION_COMPRADOR = timedelta(days=30)
TIPO_COMPRADOR = "comprador"


def crear_token_comprador(customer_uuid: str, version: int) -> str:
    """Token del comprador. Otro `tipo` que el del personal: uno no sirve
    donde va el otro."""
    ahora = datetime.now(UTC)
    payload: dict[str, Any] = {
        "sub": customer_uuid,
        "v": version,
        "iat": ahora,
        "exp": ahora + SESION_COMPRADOR,
        "tipo": TIPO_COMPRADOR,
    }
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=ALGORITMO)


def decodificar_token_comprador(token: str) -> dict[str, Any]:
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[ALGORITMO])
    except JWTError as exc:
        raise TokenInvalido("Tu sesión terminó. Vuelve a entrar.") from exc
    if payload.get("tipo") != TIPO_COMPRADOR:
        raise TokenInvalido("Tu sesión terminó. Vuelve a entrar.")
    return payload
