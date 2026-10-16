"""Esquemas de lo que ve el comprador sin sesion.

Cada campo que se agrega aqui sale a internet. Antes de sumar uno,
preguntarse si el organizador querria que su competencia lo viera.
"""

from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel


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


class ZonaDelMapaResponse(BaseModel):
    id: int
    nombre: str
    color: str | None
    precio_desde: Decimal | None

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
    # Precio final con IVA, el mismo que se cobra al apartar.
    precio: Decimal

    model_config = {"from_attributes": True}


class MapaPublicoResponse(BaseModel):
    funcion: FuncionPublicaResponse
    viewport: dict
    zonas: list[ZonaDelMapaResponse]
    butacas: list[ButacaDelMapaResponse]

    model_config = {"from_attributes": True}
