"""Esquemas de lo que ve el comprador sin sesion.

Cada campo que se agrega aqui sale a internet. Antes de sumar uno,
preguntarse si el organizador querria que su competencia lo viera.
"""

from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field

from app.core.textos import NombreDePersona


class RecintoPublicoResponse(BaseModel):
    nombre: str
    ciudad: str
    estado_geo: str | None
    pais: str
    # El front convierte las fechas, que viajan en UTC, a la hora del recinto.
    zona_horaria: str

    model_config = {"from_attributes": True}


class MedicionResponse(BaseModel):
    ga4_id: str | None
    meta_pixel_id: str | None

    model_config = {"from_attributes": True}


class FuncionPublicaResponse(BaseModel):
    id: int
    uuid: UUID
    inicia_en: datetime
    apertura_puertas: datetime | None
    estado_venta: Literal["en_venta", "proximamente", "agotada", "venta_cerrada"]
    venta_abre_en: datetime | None
    disponibilidad: Literal["disponible", "ultimos_lugares", "agotado"] | None
    # Lo que paga el comprador por el boleto mas barato libre: sin cargos
    # encima, el precio anunciado es el precio final (ADR 0012).
    precio_desde: Decimal | None

    model_config = {"from_attributes": True}


class EventoPublicoResponse(BaseModel):
    uuid: UUID
    slug: str
    nombre: str
    categoria: str | None
    descripcion: str | None
    imagen_url: str | None
    recinto: RecintoPublicoResponse
    medicion: MedicionResponse
    funciones: list[FuncionPublicaResponse]

    model_config = {"from_attributes": True}


class TarjetaDeEventoResponse(BaseModel):
    """Un evento en la cartelera. Lo minimo para la tarjeta y su link."""

    slug: str
    nombre: str
    categoria: str | None
    imagen_url: str | None
    destacado: bool
    recinto: RecintoPublicoResponse
    proxima_funcion: datetime
    # Cuantas funciones proximas tiene; no son butacas.
    funciones: int
    estado_venta: Literal["en_venta", "proximamente", "agotada", "venta_cerrada"]
    disponibilidad: Literal["disponible", "ultimos_lugares", "agotado"] | None
    precio_desde: Decimal | None

    model_config = {"from_attributes": True}


class CiudadDeCarteleraResponse(BaseModel):
    nombre: str
    pais: str
    # Cuantos eventos tiene; no son butacas.
    eventos: int

    model_config = {"from_attributes": True}


class FiltrosDeCarteleraResponse(BaseModel):
    """Las opciones de los filtros, de toda la cartelera."""

    paises: list[str]
    # De la mas buscada a la menos.
    ciudades: list[CiudadDeCarteleraResponse]
    categorias: list[str]
    # AAAA-MM, en la hora de cada recinto.
    meses: list[str]

    model_config = {"from_attributes": True}


class CarteleraResponse(BaseModel):
    eventos: list[TarjetaDeEventoResponse]
    # Cuantos eventos coinciden con la busqueda (puede ser mas que la lista).
    total: int
    filtros: FiltrosDeCarteleraResponse

    model_config = {"from_attributes": True}


class TarifaResponse(BaseModel):
    tipo: Literal["nino", "mayor"]
    nombre: str
    precio: Decimal


class ZonaDelMapaResponse(BaseModel):
    id: int
    nombre: str
    color: str | None
    precio_desde: Decimal | None
    # Tipos de boleto (TB) ademas de adulto. Vacio: solo adulto.
    tarifas: list[TarifaResponse] = []
    # Entrada general (EG): sin lugares numerados, se escoge cuantos.
    general: bool = False
    disponibles: int | None = None

    model_config = {"from_attributes": True}


class ButacaDelMapaResponse(BaseModel):
    id: int
    zona_id: int
    fila: str
    numero: str
    x: Decimal
    y: Decimal
    # libre: se puede elegir. ocupada: vendida, apartada por otra persona,
    # bloqueada o cortesia, sin decir cual. tuya: la tiene el apartado que
    # el front mando en `apartado`.
    estado: Literal["libre", "ocupada", "tuya"]
    # Precio final con IVA, el mismo que se cobra al apartar. Es el de adulto.
    precio: Decimal
    # Con precio puesto a mano: solo se vende como adulto.
    solo_adulto: bool = False

    model_config = {"from_attributes": True}


class MapaPublicoResponse(BaseModel):
    funcion: FuncionPublicaResponse
    viewport: dict
    zonas: list[ZonaDelMapaResponse]
    butacas: list[ButacaDelMapaResponse]

    model_config = {"from_attributes": True}


class VerificacionHumanaResponse(BaseModel):
    # Con activa=false el front no muestra el widget de Turnstile.
    activa: bool
    site_key: str | None


class ConfiguracionPublicaResponse(BaseModel):
    """Lo que el front necesita saber del servidor antes de vender."""

    verificacion_humana: VerificacionHumanaResponse


class CorreoEnElNavegadorResponse(BaseModel):
    asunto: str
    html: str


class ReenvioResponse(BaseModel):
    # El correo de la compra, tapado: "an•••@correo.mx".
    enviado_a: str


class PasarBoletoRequest(BaseModel):
    """BOL6: a quien le pasa el boleto quien compro."""

    # Sale en la lista de la puerta como titular.
    nombre: NombreDePersona = Field(min_length=1, max_length=120)
    apellidos: NombreDePersona = Field(min_length=1, max_length=120)
    # Ahi le llega su liga.
    email: EmailStr
