"""Esquemas de "Tus boletos"."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class BoletoResponse(BaseModel):
    uuid: UUID
    # Va impreso bajo el QR: es lo que se dicta en puerta si el escaner
    # no lee.
    codigo: str
    zona: str
    asiento: str
    estado: str
    # El front lo dibuja como QR. Nulo si el boleto ya no puede entrar.
    qr: str | None


class FuncionDelBoletoResponse(BaseModel):
    uuid: UUID
    inicia_en: datetime
    apertura_puertas: datetime | None


class EventoDelBoletoResponse(BaseModel):
    nombre: str
    slug: str


class RecintoDelBoletoResponse(BaseModel):
    nombre: str
    direccion: str | None
    ciudad: str
    # Las fechas viajan en UTC; el front las muestra a la hora del recinto.
    zona_horaria: str


class BoletosResponse(BaseModel):
    folio: str
    # Solo una orden "pagada" trae QR. Pendiente: lista vacia; reembolsada:
    # boletos sin QR. El front explica el motivo con este estado.
    estado: str
    evento: EventoDelBoletoResponse
    funcion: FuncionDelBoletoResponse
    recinto: RecintoDelBoletoResponse
    boletos: list[BoletoResponse]
