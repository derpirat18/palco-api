"""Esquemas del control de acceso (puerta)."""

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field


class RenglonDeAccesoResponse(BaseModel):
    """Sin correo ni telefono: esto se imprime y se queda en la puerta."""

    zona: str
    asiento: str
    codigo: str
    folio: str
    titular: str
    cortesia: bool
    estado: str

    model_config = {"from_attributes": True}


class ListaDeAccesoResponse(BaseModel):
    evento: str
    recinto: str
    ciudad: str
    zona_horaria: str
    inicia_en: datetime
    apertura_puertas: datetime | None
    total: int
    usados: int
    boletos: list[RenglonDeAccesoResponse]

    model_config = {"from_attributes": True}


class FuncionDePuertaResponse(BaseModel):
    """Una funcion donde esta cuenta puede escanear."""

    performance_id: int
    event_id: int
    evento: str
    recinto: str
    ciudad: str
    zona_horaria: str
    inicia_en: datetime
    apertura_puertas: datetime | None
    estado: str
    total: int
    usados: int


class EscaneoRequest(BaseModel):
    # El texto del QR o el codigo impreso, escrito a mano.
    codigo: str = Field(min_length=1, max_length=200)
    # Lo genera el celular por lectura: si reintenta, es la misma lectura.
    client_scan_id: str = Field(pattern=r"^[A-Za-z0-9_-]{8,64}$")


class DatosDelBoletoResponse(BaseModel):
    """Lo minimo para dejar pasar: sin correo ni telefono."""

    codigo: str
    folio: str
    zona: str
    asiento: str
    titular: str
    cortesia: bool

    model_config = {"from_attributes": True}


class ContadorResponse(BaseModel):
    total: int
    usados: int

    model_config = {"from_attributes": True}


class EscaneoResponse(BaseModel):
    resultado: Literal["valido", "duplicado", "otra_funcion", "invalido", "cancelado"]
    escaneado_en: datetime
    boleto: DatosDelBoletoResponse | None
    entro_en: datetime | None
    entro_con: str | None
    motivo: str | None
    su_funcion_inicia_en: datetime | None
    contador: ContadorResponse

    model_config = {"from_attributes": True}


class RenglonDeVentaResponse(BaseModel):
    folio: str
    comprada_en: datetime
    zona: str
    asiento: str
    titular: str
    precio: Decimal
    descuento: Decimal
    iva: Decimal
    total: Decimal
    canal: str
    motivo_cortesia: str | None
    estado: str
    entro_en: datetime | None
    email: str
    telefono: str | None

    model_config = {"from_attributes": True}


class ResumenDeVentasResponse(BaseModel):
    boletos: int
    cortesias: int
    importe: Decimal
    iva: Decimal
    canceladas: int

    model_config = {"from_attributes": True}


class ListadoDeVentasResponse(BaseModel):
    evento: str
    recinto: str
    zona_horaria: str
    inicia_en: datetime
    resumen: ResumenDeVentasResponse
    ventas: list[RenglonDeVentaResponse]

    model_config = {"from_attributes": True}
