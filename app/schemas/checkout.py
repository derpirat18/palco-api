"""Esquemas del checkout y de la orden que ve el comprador.

Son publicos: el comprador no tiene sesion. La orden se consulta por su
`uuid`, que funciona como el link de un correo: quien lo tiene es quien
compro. Por eso aqui no salen correo, telefono ni codigos de boleto.
"""

from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field

from app.core.textos import Linea, NombreDePersona


class CheckoutRequest(BaseModel):
    email: EmailStr
    nombre: NombreDePersona = Field(min_length=1, max_length=120)
    apellidos: NombreDePersona | None = Field(default=None, max_length=120)
    # Se normaliza y valida en el servicio (normalizar_telefono).
    telefono: Linea | None = Field(default=None, max_length=30)
    # Dos casillas separadas, nunca una sola (ADR 0009).
    acepta_aviso: bool
    acepta_marketing: bool = False
    # Version del aviso que el front mostro, p. ej. "2026-10".
    aviso_version: str = Field(min_length=1, max_length=20)

    utm_source: Linea | None = Field(default=None, max_length=120)
    utm_medium: Linea | None = Field(default=None, max_length=120)
    utm_campaign: Linea | None = Field(default=None, max_length=160)
    utm_content: Linea | None = Field(default=None, max_length=160)
    canal: Linea | None = Field(default=None, max_length=40)


class PartidaResponse(BaseModel):
    zona: str
    asiento: str
    # Precio al publico de la butaca (con IVA) y cuanto de el es IVA.
    precio: Decimal
    iva: Decimal
    importe: Decimal


class OrdenResponse(BaseModel):
    uuid: UUID
    folio: str
    estado: Literal["pendiente", "pagada", "cancelada", "reembolsada", "expirada"]
    moneda: str
    # Como en una factura: subtotal (sin IVA) + iva - descuento = total.
    subtotal: Decimal
    iva: Decimal
    iva_pct: Decimal
    descuento: Decimal
    total: Decimal
    expira_en: datetime | None
    pagada_en: datetime | None
    partidas: list[PartidaResponse]


class PagoResponse(BaseModel):
    proveedor: Literal["stripe"] = "stripe"
    # Para montar el formulario de tarjeta de Stripe en la pagina.
    client_secret: str
    publishable_key: str


class CheckoutResponse(BaseModel):
    orden: OrdenResponse
    pago: PagoResponse
