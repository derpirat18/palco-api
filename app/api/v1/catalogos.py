"""Catálogos del panel (GEO1, CAT1): ciudades y categorías de eventos."""

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.api.deps import DB, UsuarioActual, requiere_rol, verificar_acceso_evento
from app.core.errors import EVENTO_NO_EXISTE
from app.core.textos import Linea
from app.models.base import RolUsuario
from app.models.users import User
from app.services import catalogos as cat
from app.services.catalog import CatalogService

router = APIRouter(prefix="/catalogos", tags=["catalogos"])
solo_psd = Depends(requiere_rol(RolUsuario.ADMIN_PSD))
psd_u_organizador = Depends(requiere_rol(RolUsuario.ADMIN_PSD, RolUsuario.ORGANIZADOR))


class CiudadRequest(BaseModel):
    pais: Literal["MX"] = "MX"
    estado: Linea = Field(min_length=2, max_length=80)
    nombre: Linea = Field(min_length=2, max_length=80)


class CategoriaRequest(BaseModel):
    nombre: Linea = Field(min_length=2, max_length=60)
    # El evento desde el que se pide: al aprobarla se le pone.
    evento_id: int | None = None


class AprobarRequest(BaseModel):
    # PSD puede corregir cómo se escribe al aprobarla.
    nombre: Linea | None = Field(default=None, min_length=2, max_length=60)


class RechazarRequest(BaseModel):
    motivo: Linea = Field(min_length=3, max_length=200)


class CategoriaResponse(BaseModel):
    id: int
    nombre: str
    estado: str
    solicitada_en: datetime
    motivo_rechazo: str | None
    para_evento_id: int | None
    # Quién la pidió (solo para PSD).
    solicitante: str | None = None

    model_config = {"from_attributes": True}


class CategoriasResponse(BaseModel):
    aprobadas: list[CategoriaResponse]
    solicitudes: list[CategoriaResponse]


async def _categorias(db, usuario) -> CategoriasResponse:
    c = await cat.categorias(db, usuario)
    nombres = {}
    if usuario.rol == RolUsuario.ADMIN_PSD:
        ids = {s.solicitada_por for s in c.solicitudes if s.solicitada_por}
        for uid in ids:
            u = await db.get(User, uid)
            if u:
                nombres[uid] = u.nombre
    return CategoriasResponse(
        aprobadas=[CategoriaResponse.model_validate(x) for x in c.aprobadas],
        solicitudes=[
            CategoriaResponse.model_validate(x).model_copy(
                update={"solicitante": nombres.get(x.solicitada_por)}
            )
            for x in c.solicitudes
        ],
    )


@router.get("/geo")
async def geo(db: DB, _: UsuarioActual) -> list[dict]:
    """Países, estados y ciudades del catálogo, para las listas del panel."""
    return await cat.geo(db)


@router.post("/ciudades", status_code=201, dependencies=[solo_psd])
async def agregar_ciudad(
    datos: CiudadRequest, db: DB, usuario: UsuarioActual
) -> list[dict]:
    """PSD agrega una ciudad que no estaba (los recintos solo los da de alta PSD)."""
    await cat.agregar_ciudad(db, datos.pais, datos.estado, datos.nombre, usuario.id)
    await db.commit()
    return await cat.geo(db)


@router.get(
    "/categorias", response_model=CategoriasResponse, dependencies=[psd_u_organizador]
)
async def categorias(db: DB, usuario: UsuarioActual) -> CategoriasResponse:
    """Las que se pueden escoger, y las solicitudes (PSD: todas las pendientes;
    organizador: las suyas)."""
    return await _categorias(db, usuario)


@router.post(
    "/categorias",
    response_model=CategoriasResponse,
    status_code=201,
    dependencies=[psd_u_organizador],
)
async def pedir_categoria(
    datos: CategoriaRequest, db: DB, usuario: UsuarioActual
) -> CategoriasResponse:
    """PSD la agrega lista para usarse; un organizador la solicita a PSD."""
    if datos.evento_id is not None:
        evento = await CatalogService(db).obtener_evento(datos.evento_id)
        await verificar_acceso_evento(
            db, usuario, evento.id, evento.client_id, no_encontrado=EVENTO_NO_EXISTE
        )
    await cat.pedir_categoria(db, datos.nombre, usuario, datos.evento_id)
    await db.commit()
    return await _categorias(db, usuario)


@router.post(
    "/categorias/{categoria_id}/aprobar",
    response_model=CategoriasResponse,
    dependencies=[solo_psd],
)
async def aprobar(
    categoria_id: int, datos: AprobarRequest, db: DB, usuario: UsuarioActual
) -> CategoriasResponse:
    await cat.aprobar_categoria(db, categoria_id, usuario.id, datos.nombre)
    await db.commit()
    return await _categorias(db, usuario)


@router.post(
    "/categorias/{categoria_id}/rechazar",
    response_model=CategoriasResponse,
    dependencies=[solo_psd],
)
async def rechazar(
    categoria_id: int, datos: RechazarRequest, db: DB, usuario: UsuarioActual
) -> CategoriasResponse:
    await cat.rechazar_categoria(db, categoria_id, usuario.id, datos.motivo)
    await db.commit()
    return await _categorias(db, usuario)
