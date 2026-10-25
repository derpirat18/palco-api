"""Alertas por correo (A4): a quien le llegan y lo que se ha avisado. Solo PSD."""

from datetime import datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.api.deps import DB, UsuarioActual, requiere_rol
from app.core.errors import ErrorDeDominio, RecursoNoEncontrado
from app.core.textos import NombreDePersona
from app.models.alertas import Alerta, DestinatarioDeAlertas
from app.models.base import RolUsuario
from app.services.alertas import alertar
from app.services.correo import Cartero, cartero_por_configuracion

router = APIRouter(prefix="/alertas", tags=["alertas"])
solo_psd = Depends(requiere_rol(RolUsuario.ADMIN_PSD))


def get_cartero() -> Cartero:
    """Dependencia para que las pruebas usen un cartero falso."""
    return cartero_por_configuracion()


class DestinatarioRequest(BaseModel):
    email: EmailStr
    nombre: NombreDePersona | None = Field(default=None, max_length=120)


class DestinatarioResponse(BaseModel):
    id: int
    email: str
    nombre: str | None
    agregado_en: datetime

    model_config = {"from_attributes": True}


class AlertaResponse(BaseModel):
    id: int
    titulo: str
    detalle: str
    enviada_a: int
    prueba: bool
    creada_en: datetime

    model_config = {"from_attributes": True}


class AlertasResponse(BaseModel):
    destinatarios: list[DestinatarioResponse]
    recientes: list[AlertaResponse]


class DestinatarioRepetido(ErrorDeDominio):
    codigo = "destinatario_repetido"
    http_status = 409


class UltimoDestinatario(ErrorDeDominio):
    codigo = "ultimo_destinatario"
    http_status = 409


async def _todo(db) -> AlertasResponse:
    destinatarios = (
        await db.scalars(
            select(DestinatarioDeAlertas).order_by(DestinatarioDeAlertas.id)
        )
    ).all()
    recientes = (
        await db.scalars(select(Alerta).order_by(Alerta.id.desc()).limit(30))
    ).all()
    return AlertasResponse(
        destinatarios=[DestinatarioResponse.model_validate(d) for d in destinatarios],
        recientes=[AlertaResponse.model_validate(a) for a in recientes],
    )


@router.get("", response_model=AlertasResponse, dependencies=[solo_psd])
async def ver(db: DB) -> AlertasResponse:
    """Quien recibe las alertas y las ultimas 30 que salieron."""
    return await _todo(db)


@router.post(
    "/destinatarios",
    response_model=AlertasResponse,
    status_code=201,
    dependencies=[solo_psd],
)
async def agregar(
    datos: DestinatarioRequest, db: DB, usuario: UsuarioActual
) -> AlertasResponse:
    correo = str(datos.email).strip().lower()
    nuevo = await db.scalar(
        pg_insert(DestinatarioDeAlertas)
        .values(email=correo, nombre=datos.nombre or None, agregado_por=usuario.id)
        .on_conflict_do_nothing(index_elements=[DestinatarioDeAlertas.email])
        .returning(DestinatarioDeAlertas.id)
    )
    if nuevo is None:
        raise DestinatarioRepetido("Ese correo ya recibe las alertas.")
    await db.commit()
    return await _todo(db)


@router.delete(
    "/destinatarios/{destinatario_id}",
    response_model=AlertasResponse,
    dependencies=[solo_psd],
)
async def quitar(destinatario_id: int, db: DB) -> AlertasResponse:
    destinatario = await db.get(DestinatarioDeAlertas, destinatario_id)
    if destinatario is None:
        raise RecursoNoEncontrado("Ese destinatario ya no está en la lista.")
    total = len((await db.scalars(select(DestinatarioDeAlertas.id))).all())
    if total <= 1:
        raise UltimoDestinatario(
            "Debe quedar al menos un correo: si no, nadie se entera de una falla."
        )
    await db.delete(destinatario)
    await db.commit()
    return await _todo(db)


@router.post("/prueba", response_model=AlertasResponse, dependencies=[solo_psd])
async def probar(
    db: DB, usuario: UsuarioActual, cartero: Cartero = Depends(get_cartero)
) -> AlertasResponse:
    """Manda una alerta de prueba a todos, para revisar que si llegan."""
    await alertar(
        db,
        cartero,
        "prueba",
        "Alerta de prueba",
        f"La mandó {usuario.nombre} desde el panel para revisar que las alertas "
        "llegan. Si te llegó, ya estás en la lista.",
        prueba=True,
    )
    return await _todo(db)
