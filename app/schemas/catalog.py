"""Esquemas del catalogo."""

from datetime import datetime
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, Field

from app.services.seat_maps import EspecificacionDeMapa  # noqa: F401  (re-export)


class VenueCrear(BaseModel):
    nombre: str = Field(min_length=1, max_length=160)
    ciudad: str = Field(min_length=1, max_length=80)
    estado_geo: str | None = Field(default=None, max_length=80)
    pais: str = Field(default="MX", min_length=2, max_length=2)
    zona_horaria: str = Field(default="America/Mexico_City", max_length=64)
    direccion: str | None = None
    capacidad: int | None = Field(default=None, gt=0)


class VenueResponse(BaseModel):
    id: int
    nombre: str
    ciudad: str
    estado_geo: str | None
    pais: str
    zona_horaria: str
    capacidad: int | None

    model_config = {"from_attributes": True}


class SeatMapResponse(BaseModel):
    id: int
    venue_id: int
    nombre: str
    viewport: dict
    activo: bool
    total_butacas: int = 0
    total_vendibles: int = 0

    model_config = {"from_attributes": True}


class ZonaResponse(BaseModel):
    id: int
    nombre: str
    color: str | None
    orden: int

    model_config = {"from_attributes": True}


class ImportarMapaRequest(BaseModel):
    nombre: str = Field(min_length=1, max_length=120)
    csv: str = Field(min_length=1)
    ajustar_viewport: bool = True


class EventoCrear(BaseModel):
    client_id: int | None = None  # lo fija el servidor para organizadores
    venue_id: int
    nombre: str = Field(min_length=1, max_length=200)
    categoria: str | None = Field(default=None, max_length=60)
    descripcion: str | None = None
    imagen_url: str | None = Field(default=None, max_length=500)
    destacado: bool = False
    cargo_servicio_pct: Decimal | None = Field(default=None, ge=0, le=100)
    cargo_orden: Decimal | None = Field(default=None, ge=0)


class EventoActualizar(BaseModel):
    nombre: str | None = Field(default=None, min_length=1, max_length=200)
    categoria: str | None = Field(default=None, max_length=60)
    descripcion: str | None = None
    imagen_url: str | None = Field(default=None, max_length=500)
    destacado: bool | None = None
    estado: str | None = None
    cargo_servicio_pct: Decimal | None = Field(default=None, ge=0, le=100)
    cargo_orden: Decimal | None = Field(default=None, ge=0)


class EventoResponse(BaseModel):
    id: int
    client_id: int
    venue_id: int
    nombre: str
    slug: str
    categoria: str | None
    descripcion: str | None
    imagen_url: str | None
    estado: str
    destacado: bool

    model_config = {"from_attributes": True}


class FuncionCrear(BaseModel):
    seat_map_id: int
    inicia_en: datetime
    apertura_puertas: datetime | None = None
    venta_abre_en: datetime | None = None
    venta_cierra_en: datetime | None = None


class FuncionResponse(BaseModel):
    id: int
    event_id: int
    seat_map_id: int
    inicia_en: datetime
    apertura_puertas: datetime | None
    venta_abre_en: datetime | None
    estado: str
    inventario_generado: bool

    model_config = {"from_attributes": True}


# El rango lo impone tambien un CHECK en la base. Validarlo aqui es lo
# que convierte un precio negativo en un 422 claro en lugar de un 500.
Precio = Annotated[
    Decimal, Field(ge=0, le=Decimal("99999999.99"), max_digits=10, decimal_places=2)
]


class PreciosRequest(BaseModel):
    """Precio base por zona. La llave es el id de la zona."""

    precios: dict[int, Precio] = Field(min_length=1)


class PrecioResponse(BaseModel):
    id: int
    zone_id: int
    precio: Decimal
    activo: bool

    model_config = {"from_attributes": True}
