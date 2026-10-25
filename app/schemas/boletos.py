"""Esquemas de "Tus boletos"."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class BoletoResponse(BaseModel):
    uuid: UUID
    zona: str
    asiento: str
    estado: str
    # Llave (base64url) con que el celular genera el QR que cambia cada
    # 30 s (BOL3). Nula si el boleto ya no puede entrar. El codigo impreso
    # no viene: dictado o en una captura dejaria entrar sin el QR.
    llave_qr: str | None
    # Cuando entro, si ya entro (BOL8).
    entro_en: datetime | None
    # BOL6: a quien se lo paso quien compro ("Ana P."), mientras lo tenga.
    pasado_a: str | None = None
    # BOL6: hasta que abre el escaneo, quien compro puede pasarlo o recuperarlo.
    se_puede_pasar: bool = False
    se_puede_recuperar: bool = False


class FuncionDelBoletoResponse(BaseModel):
    uuid: UUID
    inicia_en: datetime
    apertura_puertas: datetime | None


class EventoDelBoletoResponse(BaseModel):
    nombre: str
    slug: str
    # El cartel, arriba de cada boleto en el celular.
    imagen_url: str | None = None


class RecintoDelBoletoResponse(BaseModel):
    nombre: str
    direccion: str | None
    ciudad: str
    # Las fechas viajan en UTC; el front las muestra a la hora del recinto.
    zona_horaria: str
    # BOL9: el punto exacto para "Como llegar", si el recinto lo tiene.
    lat: float | None = None
    lng: float | None = None


class BoletosResponse(BaseModel):
    folio: str
    # Solo una orden "pagada" trae QR. Pendiente: lista vacia; reembolsada:
    # boletos sin QR. El front explica el motivo con este estado.
    estado: str
    evento: EventoDelBoletoResponse
    funcion: FuncionDelBoletoResponse
    recinto: RecintoDelBoletoResponse
    boletos: list[BoletoResponse]
    # Para que el celular corrija su reloj al generar el QR.
    servidor_ahora: datetime
    paso_segundos: int
