"""Cuenta del comprador (R1). Ver app/services/cuenta.py.

Todo bajo /publico/cuenta. El token del comprador es de otro tipo que el
del personal: no abre el panel, y el del panel no abre esto.
"""

import logging
from datetime import datetime
from decimal import Decimal
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, Header, Request, status
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel, EmailStr, Field
from redis.asyncio.client import Redis

from app.api.deps import DB, esquema_bearer
from app.core.redis import redis_client
from app.core.security import (
    SESION_COMPRADOR,
    TokenInvalido,
    decodificar_token_comprador,
)
from app.core.telefonos import normalizar_telefono
from app.core.textos import Linea, NombreDePersona
from app.models.sales import Customer
from app.services import cuenta as svc
from app.services.consentimientos import ConsentimientoService
from app.services.correo import Cartero, Mensaje, cartero_por_configuracion
from app.services.limites import limite
from app.services.reenvio import tapar_correo
from app.services.verificacion import Verificacion, verificar_persona

router = APIRouter(prefix="/publico/cuenta", tags=["cuenta"])
log = logging.getLogger("palco.cuenta")
por_ip = limite("cuenta")


def get_redis_cuenta() -> Redis:
    """Dependencia para que las pruebas usen su propio Redis."""
    return redis_client


def get_cartero_cuenta() -> Cartero:
    """Dependencia para que las pruebas usen un cartero falso."""
    return cartero_por_configuracion()


RedisCuenta = Annotated[Redis, Depends(get_redis_cuenta)]
CarteroCuenta = Annotated[Cartero, Depends(get_cartero_cuenta)]


async def comprador_actual(
    db: DB,
    credenciales: Annotated[
        HTTPAuthorizationCredentials | None, Depends(esquema_bearer)
    ] = None,
) -> Customer:
    if credenciales is None:
        raise TokenInvalido("Entra con tu correo para continuar.")
    payload = decodificar_token_comprador(credenciales.credentials)
    return await svc.comprador_del_token(db, payload)


Comprador = Annotated[Customer, Depends(comprador_actual)]


async def _mandar(cartero: Cartero, mensaje: Mensaje) -> None:
    try:
        await cartero.enviar(mensaje)
    except Exception:  # un correo que no sale no debe tronar nada
        log.exception("No salio el correo con el codigo.")


# --------------------------------------------------------------------------
# Esquemas
# --------------------------------------------------------------------------


class PedirCodigoRequest(BaseModel):
    correo: EmailStr


class EntrarRequest(BaseModel):
    correo: EmailStr
    codigo: str = Field(min_length=1, max_length=12)


class CodigoEnviado(BaseModel):
    enviado_a: str
    minutos: int


class SesionResponse(BaseModel):
    token: str
    expira_en_segundos: int


class PerfilResponse(BaseModel):
    correo: str
    nombre: str | None
    apellidos: str | None
    telefono: str | None
    acepta_marketing: bool


class PerfilRequest(BaseModel):
    nombre: NombreDePersona | None = Field(default=None, max_length=120)
    apellidos: NombreDePersona | None = Field(default=None, max_length=120)
    telefono: Linea | None = Field(default=None, max_length=30)
    acepta_marketing: bool | None = None
    # Version del aviso que el front mostro, si cambia lo de promociones.
    aviso_version: str | None = Field(default=None, max_length=20)


class CambiarCorreoRequest(BaseModel):
    correo: EmailStr


class ConfirmarCorreoRequest(BaseModel):
    correo: EmailStr
    codigo: str = Field(min_length=1, max_length=12)


class CompraResponse(BaseModel):
    uuid: UUID
    folio: str
    estado: str
    evento: str
    imagen_url: str | None
    inicia_en: datetime
    zona_horaria: str
    recinto: str
    ciudad: str
    boletos: int
    total: Decimal
    cortesia: bool


class PaseResponse(BaseModel):
    token: str
    de: str
    evento: str
    imagen_url: str | None
    inicia_en: datetime
    zona_horaria: str
    recinto: str
    ciudad: str
    boletos: int


class ComprasResponse(BaseModel):
    compras: list[CompraResponse]
    pases: list[PaseResponse]


def _sesion(token: str) -> SesionResponse:
    return SesionResponse(
        token=token, expira_en_segundos=int(SESION_COMPRADOR.total_seconds())
    )


async def _perfil(db, c: Customer) -> PerfilResponse:
    marketing = await ConsentimientoService(db).acepta(c.id, "marketing")
    return PerfilResponse(
        correo=c.email,
        nombre=c.nombre,
        apellidos=c.apellidos,
        telefono=c.telefono,
        acepta_marketing=marketing,
    )


# --------------------------------------------------------------------------
# Entrar
# --------------------------------------------------------------------------


@router.post(
    "/codigo",
    response_model=CodigoEnviado,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[por_ip],
)
async def pedir_codigo(
    datos: PedirCodigoRequest,
    request: Request,
    tareas: BackgroundTasks,
    redis: RedisCuenta,
    cartero: CarteroCuenta,
    verificador: Verificacion,
    verificacion: Annotated[str | None, Header(alias="X-Palco-Verificacion")] = None,
) -> CodigoEnviado:
    """Manda un codigo para entrar. Sirve igual si el correo ya compro o no:
    la respuesta no dice si ya existia."""
    await verificar_persona(verificador, request, verificacion)
    correo = svc.normalizar_correo(str(datos.correo))
    codigo = await svc.nuevo_codigo(redis, svc.ENTRAR, correo)
    tareas.add_task(_mandar, cartero, svc.correo_del_codigo(correo, codigo, svc.ENTRAR))
    return CodigoEnviado(
        enviado_a=tapar_correo(correo), minutos=svc.VIDA_DEL_CODIGO // 60
    )


@router.post("/entrar", response_model=SesionResponse, dependencies=[por_ip])
async def entrar(datos: EntrarRequest, db: DB, redis: RedisCuenta) -> SesionResponse:
    correo = svc.normalizar_correo(str(datos.correo))
    await svc.comprobar_codigo(redis, svc.ENTRAR, correo, datos.codigo)
    _, token = await svc.entrar(db, correo)
    await db.commit()
    return _sesion(token)


@router.post("/salir-de-todos", response_model=SesionResponse)
async def salir_de_todos(db: DB, c: Comprador) -> SesionResponse:
    """Cierra la sesion en todos sus equipos; este sigue adentro."""
    token = await svc.salir_de_todos(db, c)
    await db.commit()
    return _sesion(token)


# --------------------------------------------------------------------------
# Perfil y compras
# --------------------------------------------------------------------------


@router.get("", response_model=PerfilResponse)
async def perfil(db: DB, c: Comprador) -> PerfilResponse:
    return await _perfil(db, c)


@router.patch("", response_model=PerfilResponse)
async def cambiar_perfil(datos: PerfilRequest, db: DB, c: Comprador) -> PerfilResponse:
    """Solo lo que venga. Vacio borra el telefono; nombre y apellidos no se
    pueden dejar vacios."""
    cambios = datos.model_dump(exclude_unset=True)
    if cambios.get("nombre"):
        c.nombre = cambios["nombre"].strip()
    if cambios.get("apellidos"):
        c.apellidos = cambios["apellidos"].strip()
    if "telefono" in cambios:
        c.telefono = normalizar_telefono(cambios["telefono"])
    if cambios.get("acepta_marketing") is not None:
        consentimientos = ConsentimientoService(db)
        ahora = await consentimientos.acepta(c.id, "marketing")
        if ahora != datos.acepta_marketing:
            await consentimientos.registrar(
                c.id,
                "marketing",
                bool(datos.acepta_marketing),
                origen="mi_cuenta",
                aviso_version=datos.aviso_version or "sin_version",
            )
    await db.flush()
    respuesta = await _perfil(db, c)
    await db.commit()
    return respuesta


@router.get("/compras", response_model=ComprasResponse)
async def compras(db: DB, c: Comprador) -> ComprasResponse:
    return ComprasResponse(
        compras=[CompraResponse(**x.__dict__) for x in await svc.compras(db, c)],
        pases=[PaseResponse(**x.__dict__) for x in await svc.pases_recibidos(db, c)],
    )


# --------------------------------------------------------------------------
# Cambiar correo
# --------------------------------------------------------------------------


@router.post(
    "/correo",
    response_model=CodigoEnviado,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[por_ip],
)
async def pedir_cambio_de_correo(
    datos: CambiarCorreoRequest,
    tareas: BackgroundTasks,
    c: Comprador,
    redis: RedisCuenta,
    cartero: CarteroCuenta,
) -> CodigoEnviado:
    """Manda un codigo al correo nuevo; el cambio se hace al confirmarlo."""
    nuevo = svc.normalizar_correo(str(datos.correo))
    if nuevo == c.email:
        raise svc.CorreoOcupado("Ese ya es tu correo.", {"campo": "correo"})
    codigo = await svc.nuevo_codigo(redis, svc.CAMBIAR, nuevo, customer_id=c.id)
    tareas.add_task(_mandar, cartero, svc.correo_del_codigo(nuevo, codigo, svc.CAMBIAR))
    return CodigoEnviado(
        enviado_a=tapar_correo(nuevo), minutos=svc.VIDA_DEL_CODIGO // 60
    )


@router.post("/correo/confirmar", response_model=SesionResponse, dependencies=[por_ip])
async def confirmar_cambio_de_correo(
    datos: ConfirmarCorreoRequest, db: DB, c: Comprador, redis: RedisCuenta
) -> SesionResponse:
    """Cambia el correo (o junta las cuentas). Devuelve la sesion con la que
    sigue: si se juntaron, es la de la cuenta del correo nuevo."""
    nuevo = svc.normalizar_correo(str(datos.correo))
    await svc.comprobar_codigo(
        redis, svc.CAMBIAR, nuevo, datos.codigo, customer_id=c.id
    )
    destino = await svc.cambiar_correo(db, c, nuevo)
    token = await svc.salir_de_todos(db, destino)
    await db.commit()
    return _sesion(token)
