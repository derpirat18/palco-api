"""Que nadie vea un error tecnico (M2, M3).

La regla de PSD: ante una falla nuestra, el usuario lee "El sistema no
esta disponible en este momento" y PSD recibe el detalle. Ante un dato
mal escrito, el usuario lee que corregir, en palabras simples.

- `AtrapaFallas`: cualquier excepcion que nadie manejo se convierte en
  un 500 con ese mensaje y un **folio**. El mismo folio queda en el
  registro con la traza completa, para que PSD encuentre el caso cuando
  el usuario lo cite.
- `cuerpo_de_validacion`: los 422 de FastAPI (en ingles, con la entrada
  del usuario adentro) se traducen al formato unico de errores, campo por
  campo y sin repetir lo que el usuario escribio.
"""

import logging
import secrets

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.respuestas import JSONUtf8

log = logging.getLogger("palco.fallas")

# Lo conecta app/main.py al arrancar (alertas por correo, A4):
# al_fallar(folio, metodo, ruta, excepcion). Aqui no se importa el servicio.
al_fallar = None

SISTEMA_NO_DISPONIBLE = (
    "El sistema no está disponible en este momento. Intenta de nuevo en unos minutos."
)
REVISA_LOS_DATOS = "Revisa los datos marcados e intenta de nuevo."


def nuevo_folio() -> str:
    return "INC-" + secrets.token_hex(4).upper()


def cuerpo_de_falla(folio: str) -> dict:
    return {
        "error": {
            "codigo": "error_interno",
            "mensaje": SISTEMA_NO_DISPONIBLE,
            "detalle": {"folio": folio},
        }
    }


class AtrapaFallas:
    """Middleware ASGI. Va por dentro de CORS para que el navegador pueda
    leer la respuesta: sin los encabezados de CORS, el front veria un
    error de red en vez del mensaje."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        empezo = False

        async def enviar(mensaje: Message) -> None:
            nonlocal empezo
            if mensaje["type"] == "http.response.start":
                empezo = True
            await send(mensaje)

        try:
            await self.app(scope, receive, enviar)
        except Exception as exc:
            folio = nuevo_folio()
            # Alerta para PSD (A1/A4): aqui esta todo lo que el usuario no ve.
            log.exception(
                "Falla sin manejar %s en %s %s",
                folio,
                scope.get("method"),
                scope.get("path"),
            )
            if al_fallar is not None:
                try:
                    al_fallar(folio, scope.get("method"), scope.get("path"), exc)
                except Exception:
                    log.exception("No se pudo avisar la falla %s", folio)
            if empezo:
                # La respuesta ya salio a medias: no hay forma de cambiarla.
                raise
            respuesta = JSONUtf8(
                status_code=500,
                content=cuerpo_de_falla(folio),
                headers={"X-Folio": folio},
            )
            await respuesta(scope, receive, send)


# --------------------------------------------------------------------------
# 422: datos que no pasan la validacion
# --------------------------------------------------------------------------

_POR_TIPO = {
    "missing": "Este dato es obligatorio.",
    "int_parsing": "Debe ser un número entero.",
    "int_type": "Debe ser un número entero.",
    "int_from_float": "Debe ser un número entero.",
    "float_parsing": "Debe ser un número.",
    "float_type": "Debe ser un número.",
    "decimal_parsing": "Debe ser un número.",
    "decimal_type": "Debe ser un número.",
    "decimal_max_places": "Tiene demasiados decimales.",
    "decimal_max_digits": "El número es demasiado grande.",
    "bool_parsing": "Debe ser verdadero o falso.",
    "bool_type": "Debe ser verdadero o falso.",
    "uuid_parsing": "El identificador no es válido.",
    "uuid_type": "El identificador no es válido.",
    "uuid_version": "El identificador no es válido.",
    "datetime_parsing": "La fecha no es válida.",
    "datetime_type": "La fecha no es válida.",
    "datetime_from_date_parsing": "La fecha no es válida.",
    "date_parsing": "La fecha no es válida.",
    "timezone_aware": "La fecha debe incluir su zona horaria.",
    "json_invalid": "El contenido no se pudo leer.",
    "literal_error": "El valor no está entre las opciones permitidas.",
    "enum": "El valor no está entre las opciones permitidas.",
    "string_type": "Debe ser texto.",
    "list_type": "Debe ser una lista.",
    "dict_type": "El formato no es válido.",
    "model_attributes_type": "El formato no es válido.",
}


def _mensaje_de_campo(error: dict) -> str:
    tipo = error.get("type", "")
    ctx = error.get("ctx") or {}
    campo = str(error.get("loc", [""])[-1])
    if tipo == "string_too_short":
        n = ctx.get("min_length")
        return (
            "Este dato es obligatorio."
            if n == 1
            else f"Debe tener al menos {n} caracteres."
        )
    if tipo == "string_too_long":
        return f"Debe tener como máximo {ctx.get('max_length')} caracteres."
    if tipo == "too_short":
        return f"Debe tener al menos {ctx.get('min_length')} elementos."
    if tipo == "too_long":
        return f"Debe tener como máximo {ctx.get('max_length')} elementos."
    if tipo == "greater_than":
        return f"Debe ser mayor que {ctx.get('gt')}."
    if tipo == "greater_than_equal":
        return f"Debe ser al menos {ctx.get('ge')}."
    if tipo == "less_than":
        return f"Debe ser menor que {ctx.get('lt')}."
    if tipo == "less_than_equal":
        return f"Debe ser como máximo {ctx.get('le')}."
    if tipo == "value_error":
        if "email" in campo or "email" in str(error.get("msg", "")).lower():
            return "Escribe un correo válido, por ejemplo nombre@correo.com."
        # Los ValueError de nuestros esquemas ya estan en espanol.
        texto = str(error.get("msg", ""))
        return texto.removeprefix("Value error, ") or "El valor no es válido."
    return _POR_TIPO.get(tipo, "El valor no es válido.")


def cuerpo_de_validacion(errores: list[dict]) -> dict:
    campos = []
    for e in errores:
        ruta = [
            str(p)
            for p in e.get("loc", ())
            if p not in ("body", "query", "path", "header", "cookie")
        ]
        campos.append(
            {"campo": ".".join(ruta) or "solicitud", "mensaje": _mensaje_de_campo(e)}
        )
    return {
        "error": {
            "codigo": "datos_invalidos",
            "mensaje": REVISA_LOS_DATOS,
            "detalle": {"campos": campos},
        }
    }


# --------------------------------------------------------------------------
# Errores HTTP de Starlette (ruta inexistente, metodo no permitido)
# --------------------------------------------------------------------------

_POR_ESTADO_HTTP = {
    404: ("no_encontrado", "No encontramos lo que buscas."),
    405: ("metodo_no_permitido", "Esta acción no está disponible."),
}


def cuerpo_de_error_http(status_code: int) -> dict:
    codigo, mensaje = _POR_ESTADO_HTTP.get(
        status_code, ("solicitud_invalida", "No pudimos procesar tu solicitud.")
    )
    return {"error": {"codigo": codigo, "mensaje": mensaje, "detalle": {}}}
