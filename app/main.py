"""Punto de entrada de la API."""

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.v1.router import api_router
from app.core.config import settings
from app.core.errors import ErrorDeDominio
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

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


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


# Los dos handlers que siguen reemplazan a los de FastAPI solo para que
# sus respuestas tambien declaren charset=utf-8. El cuerpo es el mismo
# que el de FastAPI; unificarlo con el formato de ErrorDeDominio es la
# otra mitad de #44 y va con el catalogo de mensajes.


@app.exception_handler(StarletteHTTPException)
async def manejar_error_http(_: Request, exc: StarletteHTTPException) -> Response:
    headers = getattr(exc, "headers", None)
    if exc.status_code in {204, 304}:
        return Response(status_code=exc.status_code, headers=headers)
    return JSONUtf8(
        status_code=exc.status_code, content={"detail": exc.detail}, headers=headers
    )


@app.exception_handler(RequestValidationError)
async def manejar_error_de_validacion(
    _: Request, exc: RequestValidationError
) -> JSONUtf8:
    return JSONUtf8(status_code=422, content={"detail": jsonable_encoder(exc.errors())})


app.include_router(api_router, prefix=settings.API_V1_PREFIX)
