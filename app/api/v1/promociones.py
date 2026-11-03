"""Promociones del panel (PR1). Reglas en app/services/promociones.py.

PSD y el organizador. El organizador solo ve y edita las suyas; PSD todas,
y es el unico que pone una comision especial.
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.api.deps import DB, UsuarioActual, requiere_rol, verificar_acceso_cliente
from app.core.errors import PROMOCION_NO_EXISTE
from app.core.textos import Linea
from app.models.base import RolUsuario
from app.models.catalog import Client
from app.models.users import User
from app.services import promociones as svc

router = APIRouter(prefix="/promociones", tags=["promociones"])
psd_u_organizador = Depends(requiere_rol(RolUsuario.ADMIN_PSD, RolUsuario.ORGANIZADOR))


class EventoDePromocion(BaseModel):
    event_id: int
    # Vacio (None): todas las zonas del evento.
    zonas: list[int] | None = Field(default=None, max_length=200)


class PromocionRequest(BaseModel):
    # Solo PSD lo escoge; el organizador siempre crea para el suyo.
    client_id: int | None = None
    nombre: Linea = Field(min_length=3, max_length=60)
    tipo: Literal["2x1", "porcentaje"]
    porcentaje: Decimal | None = Field(default=None, ge=1, le=90, decimal_places=2)
    dias: list[int] = Field(min_length=1, max_length=7)
    todos_los_tipos: bool = False
    en_linea: bool = True
    en_taquilla: bool = True
    desde: date | None = None
    hasta: date | None = None
    todos_los_eventos: bool = True
    eventos: list[EventoDePromocion] = Field(default_factory=list, max_length=200)
    # Solo PSD. Vacio: la comision de siempre.
    comision_pct: Decimal | None = Field(default=None, ge=0, le=100, decimal_places=2)
    activa: bool = True

    def datos(self) -> svc.Datos:
        return svc.Datos(
            nombre=self.nombre,
            tipo=self.tipo,
            porcentaje=self.porcentaje,
            dias=self.dias,
            todos_los_tipos=self.todos_los_tipos,
            en_linea=self.en_linea,
            en_taquilla=self.en_taquilla,
            desde=self.desde,
            hasta=self.hasta,
            todos_los_eventos=self.todos_los_eventos,
            eventos=[(e.event_id, e.zonas) for e in self.eventos],
            activa=self.activa,
        )


class EventoDePromocionResponse(BaseModel):
    event_id: int
    nombre: str
    zonas: list[int] | None
    zonas_texto: str


class FuncionDePromocion(BaseModel):
    performance_id: int
    evento: str
    ciudad: str
    # Hora del recinto, sin zona.
    inicia_en: str
    zonas: str


class MesDePromocion(BaseModel):
    boletos: int
    descontado: Decimal
    vendido: Decimal


class PromocionResponse(BaseModel):
    id: int
    client_id: int
    organizador: str | None
    nombre: str
    tipo: str
    porcentaje: Decimal | None
    valor: str
    dias: list[int]
    dias_texto: str
    todos_los_tipos: bool
    en_linea: bool
    en_taquilla: bool
    desde: date | None
    hasta: date | None
    todos_los_eventos: bool
    eventos: list[EventoDePromocionResponse]
    comision_pct: Decimal | None
    activa: bool
    usada: bool
    proxima: FuncionDePromocion | None
    funciones_proximas: int
    actualizada_por: str | None
    actualizado_en: datetime
    mes: MesDePromocion


class ConflictoResponse(BaseModel):
    id: int
    nombre: str
    activa: bool
    valor: str


class VistaPreviaResponse(BaseModel):
    funciones: list[FuncionDePromocion]
    total_funciones: int
    conflictos: list[ConflictoResponse]


class GrupoDeZonas(BaseModel):
    nombre: str
    ids: list[int]


class EventoOpcion(BaseModel):
    event_id: int
    nombre: str
    recinto: str
    ciudad: str
    funciones: int
    zonas: list[GrupoDeZonas]


class OrganizadorOpcion(BaseModel):
    id: int
    nombre: str


class OpcionesResponse(BaseModel):
    organizadores: list[OrganizadorOpcion]
    client_id: int | None
    eventos: list[EventoOpcion]


class ActivaRequest(BaseModel):
    activa: bool


def _de_quien(usuario: User, pedido: int | None) -> int | None:
    """El organizador siempre trabaja con el suyo; PSD con el que escoja."""
    if usuario.rol == RolUsuario.ADMIN_PSD:
        return pedido
    return usuario.client_id


async def _requiere_cliente(db, usuario: User, pedido: int | None) -> int:
    client_id = _de_quien(usuario, pedido)
    if client_id is None:
        raise svc.PromocionInvalida(
            "Escoge el organizador de la promoción.", {"campo": "client_id"}
        )
    if await db.get(Client, client_id) is None:
        raise svc.PromocionInvalida(
            "Escoge el organizador de la promoción.", {"campo": "client_id"}
        )
    return client_id


async def _propia(db, usuario: User, promocion_id: int):
    promo = await svc.cargar(db, promocion_id)
    verificar_acceso_cliente(
        usuario, promo.client_id, no_encontrado=PROMOCION_NO_EXISTE
    )
    return promo


@router.get(
    "", response_model=list[PromocionResponse], dependencies=[psd_u_organizador]
)
async def listar(
    db: DB, usuario: UsuarioActual, client_id: int | None = None
) -> list[dict]:
    return await svc.listar(db, _de_quien(usuario, client_id))


@router.get(
    "/opciones", response_model=OpcionesResponse, dependencies=[psd_u_organizador]
)
async def opciones(
    db: DB, usuario: UsuarioActual, client_id: int | None = None
) -> OpcionesResponse:
    """Los eventos y zonas para escoger; PSD tambien la lista de organizadores."""
    organizadores = []
    if usuario.rol == RolUsuario.ADMIN_PSD:
        organizadores = [
            OrganizadorOpcion(id=i, nombre=n)
            for i, n in (
                await db.execute(
                    select(Client.id, Client.nombre)
                    .where(Client.activo.is_(True))
                    .order_by(Client.nombre)
                )
            ).all()
        ]
    cid = _de_quien(usuario, client_id)
    eventos = (await svc.opciones(db, cid))["eventos"] if cid else []
    return OpcionesResponse(
        organizadores=organizadores,
        client_id=cid,
        eventos=[EventoOpcion(**e) for e in eventos],
    )


@router.post(
    "/vista-previa",
    response_model=VistaPreviaResponse,
    dependencies=[psd_u_organizador],
)
async def vista_previa(
    datos: PromocionRequest,
    db: DB,
    usuario: UsuarioActual,
    promocion_id: int | None = None,
) -> dict:
    """Las funciones que caerian en la promocion y con cuales otras chocaria."""
    client_id = await _requiere_cliente(db, usuario, datos.client_id)
    if promocion_id is not None:
        await _propia(db, usuario, promocion_id)
    return await svc.vista_previa(db, client_id, datos.datos(), promocion_id)


@router.post(
    "",
    response_model=PromocionResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[psd_u_organizador],
)
async def crear(datos: PromocionRequest, db: DB, usuario: UsuarioActual) -> dict:
    client_id = await _requiere_cliente(db, usuario, datos.client_id)
    promo = await svc.guardar(
        db,
        usuario,
        client_id,
        datos.datos(),
        comision=("comision_pct" in datos.model_fields_set, datos.comision_pct),
    )
    await db.commit()
    return await svc.vista(db, promo)


@router.get(
    "/{promocion_id}",
    response_model=PromocionResponse,
    dependencies=[psd_u_organizador],
)
async def ver(promocion_id: int, db: DB, usuario: UsuarioActual) -> dict:
    return await svc.vista(db, await _propia(db, usuario, promocion_id))


@router.put(
    "/{promocion_id}",
    response_model=PromocionResponse,
    dependencies=[psd_u_organizador],
)
async def editar(
    promocion_id: int, datos: PromocionRequest, db: DB, usuario: UsuarioActual
) -> dict:
    """Reemplaza la promocion. Las compras ya hechas no cambian: congelaron
    su descuento."""
    promo = await _propia(db, usuario, promocion_id)
    promo = await svc.guardar(
        db,
        usuario,
        promo.client_id,
        datos.datos(),
        promo,
        comision=("comision_pct" in datos.model_fields_set, datos.comision_pct),
    )
    await db.commit()
    return await svc.vista(db, promo)


@router.patch(
    "/{promocion_id}/activa",
    response_model=PromocionResponse,
    dependencies=[psd_u_organizador],
)
async def encender(
    promocion_id: int, datos: ActivaRequest, db: DB, usuario: UsuarioActual
) -> dict:
    promo = await _propia(db, usuario, promocion_id)
    promo.activa = datos.activa
    promo.actualizada_por = usuario.id
    await db.commit()
    await db.refresh(promo)
    return await svc.vista(db, promo)


@router.delete(
    "/{promocion_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[psd_u_organizador],
)
async def borrar(promocion_id: int, db: DB, usuario: UsuarioActual) -> Response:
    """Solo si nunca se uso; si ya se uso, se apaga."""
    await svc.borrar(db, await _propia(db, usuario, promocion_id))
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
