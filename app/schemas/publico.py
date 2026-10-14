"""Esquemas de lo que ve el comprador sin sesion.

Cada campo que se agrega aqui sale a internet. Antes de sumar uno,
preguntarse si el organizador querria que su competencia lo viera.
"""

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel


class RecintoPublicoResponse(BaseModel):
    nombre: str
    ciudad: str
    estado_geo: str | None
    pais: str
    # El front convierte las fechas, que viajan en UTC, a la hora del recinto.
    zona_horaria: str

    model_config = {"from_attributes": True}


class CargosResponse(BaseModel):
    servicio_pct: Decimal
    por_orden: Decimal

    model_config = {"from_attributes": True}


class FuncionPublicaResponse(BaseModel):
    id: int
    inicia_en: datetime
    apertura_puertas: datetime | None
    estado_venta: Literal["en_venta", "proximamente", "agotada", "venta_cerrada"]
    venta_abre_en: datetime | None
    disponibilidad: Literal["disponible", "ultimos_lugares", "agotado"] | None
    precio_desde: Decimal | None
    # Con cargo por servicio. El cargo por orden se suma una vez al pagar.
    precio_desde_con_cargo: Decimal | None

    model_config = {"from_attributes": True}


class EventoPublicoResponse(BaseModel):
    slug: str
    nombre: str
    categoria: str | None
    descripcion: str | None
    imagen_url: str | None
    recinto: RecintoPublicoResponse
    cargos: CargosResponse
    funciones: list[FuncionPublicaResponse]

    model_config = {"from_attributes": True}
