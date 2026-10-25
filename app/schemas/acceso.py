"""Esquemas del control de acceso (puerta)."""

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field

from app.core.textos import Linea


class RenglonDeAccesoResponse(BaseModel):
    """Sin correo ni telefono: esto se imprime y se queda en la puerta.

    El codigo del boleto solo lo ven PSD y el organizador (taquilla): con
    el, tecleado, se entra sin QR. Al personal de acceso le llega nulo.
    """

    zona: str
    asiento: str
    codigo: str | None
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
    # Horario de escaneo (E3c): fuera de el, una lectura no toca el boleto.
    escaneo_abre_en: datetime
    escaneo_cierra_en: datetime


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
    resultado: Literal[
        "valido",
        "duplicado",
        "otra_funcion",
        "invalido",
        "cancelado",
        "fuera_de_horario",
    ]
    escaneado_en: datetime
    boleto: DatosDelBoletoResponse | None
    entro_en: datetime | None
    entro_con: str | None
    motivo: str | None
    su_funcion_inicia_en: datetime | None
    contador: ContadorResponse
    abre_en: datetime | None
    cierra_en: datetime | None

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
    codigo: str | None

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


class DeshacerEntradaRequest(BaseModel):
    # El codigo impreso del boleto (columna Código de Ventas).
    codigo: str = Field(min_length=1, max_length=40)
    # Por que: queda con la constancia ("se escaneo el de la funcion de mañana").
    motivo: Linea = Field(min_length=3, max_length=200)


class DeshacerEntradaResponse(BaseModel):
    codigo: str
    contador: ContadorResponse


# --------------------------------------------------------------- sin red (E3b)


class BoletoDelPaqueteResponse(BaseModel):
    u: str
    k: str | None
    estado: Literal["valido", "usado", "cancelado"]
    motivo: str | None
    zona: str
    asiento: str
    titular: str
    folio: str
    cortesia: bool
    entro_en: datetime | None

    model_config = {"from_attributes": True}


class PaqueteSinRedResponse(BaseModel):
    """Lo que el escaner guarda para seguir revisando boletos sin internet."""

    performance_id: int
    evento: str
    recinto: str
    ciudad: str
    zona_horaria: str
    inicia_en: datetime
    escaneo_abre_en: datetime
    escaneo_cierra_en: datetime
    servidor_ahora: datetime
    paso_segundos: int
    tolerancia_pasos: int
    boletos: list[BoletoDelPaqueteResponse]

    model_config = {"from_attributes": True}


class LecturaSinRed(BaseModel):
    client_scan_id: str = Field(pattern=r"^[A-Za-z0-9_-]{8,64}$")
    codigo: str = Field(min_length=1, max_length=200)
    # La hora del celular, ya corregida con la del servidor.
    escaneado_en: datetime


class SubirLecturasRequest(BaseModel):
    lecturas: list[LecturaSinRed] = Field(min_length=1, max_length=500)


class LecturaSubidaResponse(BaseModel):
    client_scan_id: str
    # Lo que dice el servidor. Si el celular dejo pasar y aqui sale
    # "duplicado", ese boleto entro dos veces (dos puertas sin red).
    escaneo: EscaneoResponse | None
    # "muy_vieja": de hace mas de un dia, no se tomo en cuenta.
    rechazo: str | None = None


class SubirLecturasResponse(BaseModel):
    resultados: list[LecturaSubidaResponse]
    contador: ContadorResponse


class ReenviarLigaRequest(BaseModel):
    """BOL13: el folio de la compra (columna Folio de Ventas)."""

    folio: str = Field(min_length=4, max_length=20)


class ReenviarLigaResponse(BaseModel):
    folio: str
    # El correo de la compra, tapado: "an•••@correo.mx".
    enviado_a: str
