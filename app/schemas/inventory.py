"""Esquemas del inventario."""

from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, Field

# Mismo criterio que en precios de zona: Numeric(10,2) en la base, y
# validar los decimales aqui evita redondeos silenciosos sobre importes.
Precio = Annotated[
    Decimal, Field(ge=0, le=Decimal("99999999.99"), max_digits=10, decimal_places=2)
]

# Tope por peticion. Mas que esto es una operacion masiva y deberia ir por
# zona, no por lista de identificadores.
MAX_BUTACAS_POR_OPERACION = 2_000


class MaterializacionResponse(BaseModel):
    performance_id: int
    butacas_creadas: int
    vendibles: int
    kill: int


class SincronizacionResponse(BaseModel):
    performance_id: int
    butacas_agregadas: int


class ResumenResponse(BaseModel):
    performance_id: int
    inventario_generado: bool
    total: int
    disponibles: int
    por_estado: dict[str, int]


class CambioDeEstadoRequest(BaseModel):
    performance_seat_ids: list[int] = Field(
        min_length=1, max_length=MAX_BUTACAS_POR_OPERACION
    )
    estado: str
    # Obligatorio: sin motivo la bitacora no sirve para resolver una
    # disputa en taquilla, que es justo para lo que existe.
    motivo: str = Field(min_length=3, max_length=80)


class CambioDeEstadoResponse(BaseModel):
    butacas_afectadas: int


class PrecioPorButacaRequest(BaseModel):
    performance_seat_ids: list[int] = Field(
        min_length=1, max_length=MAX_BUTACAS_POR_OPERACION
    )
    precio: Precio


class FuncionEstadoResponse(BaseModel):
    id: int
    estado: str
    inventario_generado: bool

    model_config = {"from_attributes": True}
