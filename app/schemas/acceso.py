"""Esquemas del control de acceso (puerta)."""

from datetime import datetime

from pydantic import BaseModel


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
