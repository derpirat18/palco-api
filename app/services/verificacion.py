"""Verificacion humana con Cloudflare Turnstile (B3 del plan; ADR 0019).

El front muestra el widget de Turnstile y manda el token que este le da
en el encabezado `X-Palco-Verificacion`. Aqui se le pregunta a
Cloudflare si ese token es de una persona.

Reglas:

- Se pide al **empezar** un apartado. Una vez aprobado, ese `hold_id`
  queda marcado en Redis mientras pueda vivir el apartado: sumar o quitar
  butacas, reintentar tras "alguien mas eligio ese lugar" y pagar ya no
  la piden. El token de Turnstile sirve una sola vez; pedirlo en cada clic
  obligaria al front a regenerarlo a cada rato.
- Token ausente, rechazado, vencido o ya usado: 403
  `verificacion_requerida`. El front vuelve a mostrar el widget y reintenta.
- Si Cloudflare no responde, falla o rechaza **nuestra** llave, se deja
  pasar y se avisa a PSD en el registro: es una falla nuestra, no del
  comprador, y no debe tumbar la venta. Los limites (ADR 0016) y la puerta
  del turno (ADR 0017) siguen protegiendo mientras tanto.
- Apagada mientras `TURNSTILE_SECRET_KEY` este vacia (local y pruebas).
  Fuera de local la configuracion exige las llaves.
"""

import logging
from enum import Enum
from typing import Annotated

import httpx
from fastapi import Depends, Request
from redis.asyncio.client import Redis
from redis.exceptions import RedisError

from app.core.config import settings
from app.core.errors import ErrorDeDominio
from app.services.limites import ip_de

log = logging.getLogger("palco.seguridad")

ENCABEZADO = "X-Palco-Verificacion"
URL_DE_CLOUDFLARE = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
# Un comprador no debe esperar mas que esto por la verificacion.
TIEMPO_MAXIMO = 4.0
# Cloudflare documenta tokens de hasta 2048 caracteres.
LARGO_MAXIMO = 2048

MENSAJE = (
    "No pudimos confirmar que eres una persona. "
    "Completa la verificación e intenta de nuevo."
)

# Codigos con los que Cloudflare dice que el problema es nuestro (llave
# mal puesta, peticion mal armada o falla de ellos), no del comprador.
_FALLAS_NUESTRAS = {
    "missing-input-secret",
    "invalid-input-secret",
    "bad-request",
    "internal-error",
}


class VerificacionRequerida(ErrorDeDominio):
    codigo = "verificacion_requerida"
    http_status = 403


class Resultado(Enum):
    APROBADO = "aprobado"
    RECHAZADO = "rechazado"
    # Cloudflare no contesto, fallo o rechazo nuestra llave.
    SIN_SERVICIO = "sin_servicio"


def activa() -> bool:
    return bool(settings.TURNSTILE_SECRET_KEY)


class Verificador:
    """Habla con Cloudflare. Las pruebas le pasan un transporte simulado."""

    def __init__(self, transporte: httpx.AsyncBaseTransport | None = None) -> None:
        self.transporte = transporte

    async def verificar(self, token: str, ip: str) -> Resultado:
        datos = {
            "secret": settings.TURNSTILE_SECRET_KEY,
            "response": token,
            "remoteip": ip,
        }
        try:
            async with httpx.AsyncClient(
                transport=self.transporte, timeout=TIEMPO_MAXIMO
            ) as cliente:
                r = await cliente.post(URL_DE_CLOUDFLARE, data=datos)
            r.raise_for_status()
            cuerpo = r.json()
            if not isinstance(cuerpo, dict):
                raise ValueError("respuesta sin forma de objeto")
        except (httpx.HTTPError, ValueError) as exc:
            # Alerta para PSD (A4): la verificacion humana esta apagada de
            # hecho mientras esto siga pasando.
            log.error("Turnstile no respondio: %s", type(exc).__name__)
            return Resultado.SIN_SERVICIO

        if cuerpo.get("success") is True:
            return Resultado.APROBADO
        codigos = set(cuerpo.get("error-codes") or [])
        if codigos & _FALLAS_NUESTRAS:
            log.error("Turnstile rechazo la peticion: %s", sorted(codigos))
            return Resultado.SIN_SERVICIO
        log.info("Turnstile no aprobo un token: %s", sorted(codigos))
        return Resultado.RECHAZADO


_verificador = Verificador()


def get_verificador() -> Verificador:
    """Dependencia para que las pruebas simulen a Cloudflare."""
    return _verificador


Verificacion = Annotated[Verificador, Depends(get_verificador)]


def _clave(hold_id: str) -> str:
    return f"humano:{hold_id}"


async def verificar_humano(
    redis: Redis,
    verificador: Verificador,
    request: Request,
    hold_id: str,
    token: str | None,
) -> None:
    """Deja empezar un apartado solo si Turnstile aprueba el token."""
    if not activa():
        return
    try:
        if await redis.exists(_clave(hold_id)):
            return
    except RedisError:
        log.warning("Marca de verificacion sin Redis: se verifica de nuevo.")

    token = (token or "").strip()
    if not token or len(token) > LARGO_MAXIMO:
        raise VerificacionRequerida(MENSAJE)

    resultado = await verificador.verificar(token, ip_de(request))
    if resultado is Resultado.RECHAZADO:
        raise VerificacionRequerida(MENSAJE)

    vida = settings.HOLD_TTL_SEGUNDOS + settings.HOLD_TTL_CHECKOUT_SEGUNDOS
    try:
        await redis.set(_clave(hold_id), "1", ex=vida)
    except RedisError:
        log.warning("No se pudo marcar el apartado como verificado.")
