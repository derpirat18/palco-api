"""Encabezados de seguridad (B7) y tamano maximo de peticion (B12).

Los dos son middlewares ASGI puros: no tocan el cuerpo de las respuestas
ni la logica de la API, solo la envoltura.
"""

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.config import settings
from app.core.respuestas import JSONUtf8

# La documentacion interactiva carga scripts de un CDN: con la politica
# estricta no se veria. Fuera de local ni siquiera existe (ver main.py).
_RUTAS_DE_DOCUMENTACION = ("/docs", "/redoc", "/openapi.json")


class EncabezadosDeSeguridad:
    """Lo que el navegador debe saber de cada respuesta de la API.

    - `nosniff`: un JSON no se interpreta como otra cosa.
    - `DENY` y `frame-ancestors 'none'`: nadie mete la API en un iframe.
    - `default-src 'none'`: una respuesta de la API no ejecuta nada.
    - `no-referrer`: los enlaces no filtran la URL de donde salieron (un
      uuid de orden, por ejemplo).
    - `no-store`: ni el navegador ni un proxy guardan respuestas con
      ordenes o boletos. La que deba guardarse lo dira ella misma.
    - HSTS solo fuera de local: obliga HTTPS, y en local no lo hay.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self.hsts = settings.ENTORNO != "local"

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        es_documentacion = scope.get("path", "").startswith(_RUTAS_DE_DOCUMENTACION)

        async def enviar(mensaje: Message) -> None:
            if mensaje["type"] == "http.response.start":
                encabezados = list(mensaje.get("headers", []))
                presentes = {k.lower() for k, _ in encabezados}

                def poner(nombre: str, valor: str) -> None:
                    if nombre.encode() not in presentes:
                        encabezados.append((nombre.encode(), valor.encode()))

                poner("x-content-type-options", "nosniff")
                poner("x-frame-options", "DENY")
                poner("referrer-policy", "no-referrer")
                poner("cache-control", "no-store")
                if not es_documentacion:
                    poner(
                        "content-security-policy",
                        "default-src 'none'; frame-ancestors 'none'",
                    )
                if self.hsts:
                    poner(
                        "strict-transport-security",
                        "max-age=31536000; includeSubDomains",
                    )
                mensaje = {**mensaje, "headers": encabezados}
            await send(mensaje)

        await self.app(scope, receive, enviar)


def _respuesta_413() -> JSONUtf8:
    return JSONUtf8(
        status_code=413,
        content={
            "error": {
                "codigo": "solicitud_muy_grande",
                "mensaje": (
                    "El archivo o los datos que enviaste son demasiado grandes."
                ),
                "detalle": {"maximo_bytes": settings.MAX_CUERPO_BYTES},
            }
        },
    )


class LimiteDeCuerpo:
    """Rechaza peticiones de mas de `MAX_CUERPO_BYTES`.

    Sin esto, alguien manda un cuerpo de 2 GB y el servidor intenta leerlo
    entero en memoria. Con `Content-Length` se decide antes de leer nada
    (el servidor no deja pasar mas bytes de los declarados). Sin el (envio
    por partes) se lee hasta el limite y, si cabe, se le entrega a la API
    tal cual. El limite es holgado: un mapa de 50,000 butacas en CSV cabe
    con margen.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        maximo = settings.MAX_CUERPO_BYTES
        largo = None
        for nombre, valor in scope.get("headers", []):
            if nombre == b"content-length":
                try:
                    largo = int(valor)
                except ValueError:
                    largo = maximo + 1
        if largo is not None:
            if largo > maximo:
                await _respuesta_413()(scope, receive, send)
                return
            await self.app(scope, receive, send)
            return

        # Sin Content-Length: se junta el cuerpo, con tope, antes de pasarlo.
        partes: list[bytes] = []
        recibido = 0
        while True:
            mensaje = await receive()
            if mensaje["type"] != "http.request":
                break  # el cliente se desconecto
            trozo = mensaje.get("body", b"")
            recibido += len(trozo)
            if recibido > maximo:
                await _respuesta_413()(scope, receive, send)
                return
            partes.append(trozo)
            if not mensaje.get("more_body", False):
                break

        entregado = False

        async def recibir() -> Message:
            nonlocal entregado
            if not entregado:
                entregado = True
                return {
                    "type": "http.request",
                    "body": b"".join(partes),
                    "more_body": False,
                }
            return await receive()

        await self.app(scope, recibir, send)
