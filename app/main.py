"""Punto de entrada de la API."""

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.v1.router import api_router
from app.core.config import settings
from app.core.errors import ErrorDeDominio
from app.core.redis import redis_client


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    await redis_client.aclose()


app = FastAPI(
    title=settings.APP_NAME,
    version="0.1.0",
    docs_url="/docs" if settings.ENTORNO != "prod" else None,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(ErrorDeDominio)
async def manejar_error_de_dominio(_: Request, exc: ErrorDeDominio) -> JSONResponse:
    """Formato unico de error para toda la API."""
    return JSONResponse(
        status_code=exc.http_status,
        content={
            "error": {
                "codigo": exc.codigo,
                "mensaje": exc.mensaje,
                "detalle": exc.detalle,
            }
        },
    )


app.include_router(api_router, prefix=settings.API_V1_PREFIX)
