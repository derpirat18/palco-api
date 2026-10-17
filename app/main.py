"""Punto de entrada de la API."""

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.v1.router import api_router
from app.core.config import settings
from app.core.errors import ErrorDeDominio
from app.core.fallas import (
    AtrapaFallas,
    cuerpo_de_error_http,
    cuerpo_de_validacion,
)
from app.core.proteccion import EncabezadosDeSeguridad, LimiteDeCuerpo
from app.core.redis import redis_client
from app.core.respuestas import JSONUtf8


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    await redis_client.aclose()


app = FastAPI(
    title=settings.APP_NAME,
    version="0.1.0",
    docs_url="/docs" if settings.ENTORNO != "prod" else None,
    lifespan=lifespan,
    # Cada respuesta de exito declara charset=utf-8 (#44).
    default_response_class=JSONUtf8,
)

# El ultimo agregado queda por fuera. De fuera hacia dentro:
#   encabezados de seguridad -> CORS -> atrapa fallas -> limite de cuerpo
# Asi toda respuesta, tambien un 500 o un 413, lleva CORS (el navegador
# puede leerla) y los encabezados de seguridad.
app.add_middleware(LimiteDeCuerpo)
app.add_middleware(AtrapaFallas)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(EncabezadosDeSeguridad)


@app.exception_handler(ErrorDeDominio)
async def manejar_error_de_dominio(_: Request, exc: ErrorDeDominio) -> JSONUtf8:
    """Formato unico de error para toda la API."""
    return JSONUtf8(
        status_code=exc.http_status,
        headers=exc.headers,
        content={
            "error": {
                "codigo": exc.codigo,
                "mensaje": exc.mensaje,
                "detalle": exc.detalle,
            }
        },
    )


# Formato unico de error tambien para lo que levanta FastAPI o Starlette
# (ruta inexistente, metodo no permitido, datos invalidos). Nunca sale el
# texto en ingles del framework ni la entrada del usuario (M3).


@app.exception_handler(StarletteHTTPException)
async def manejar_error_http(_: Request, exc: StarletteHTTPException) -> Response:
    headers = getattr(exc, "headers", None)
    if exc.status_code in {204, 304}:
        return Response(status_code=exc.status_code, headers=headers)
    return JSONUtf8(
        status_code=exc.status_code,
        content=cuerpo_de_error_http(exc.status_code),
        headers=headers,
    )


@app.exception_handler(RequestValidationError)
async def manejar_error_de_validacion(
    _: Request, exc: RequestValidationError
) -> JSONUtf8:
    return JSONUtf8(status_code=422, content=cuerpo_de_validacion(list(exc.errors())))


app.include_router(api_router, prefix=settings.API_V1_PREFIX)
