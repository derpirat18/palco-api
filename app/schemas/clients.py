"""Esquemas de organizadores.

Las condiciones comerciales son de PSD, no del organizador: viven aqui
para poder leerlas, pero quien las edita se decide en el router.
"""

from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, Field, field_validator

# Numeric(5,2) y Numeric(10,2) en la base. Validar aqui los decimales
# evita que un 10.555 se redondee en silencio: sobre una comision, ese
# redondeo se multiplica por cada boleto vendido.
Porcentaje = Annotated[Decimal, Field(ge=0, le=100, max_digits=5, decimal_places=2)]
Importe = Annotated[
    Decimal, Field(ge=0, le=Decimal("99999999.99"), max_digits=10, decimal_places=2)
]


class _NormalizaIdentificadores(BaseModel):
    @field_validator("rfc", check_fields=False)
    @classmethod
    def _rfc(cls, valor: str | None) -> str | None:
        if valor is None:
            return None
        rfc = valor.strip().upper().replace("-", "").replace(" ", "")
        # 12 para persona moral, 13 para fisica.
        if len(rfc) not in (12, 13):
            raise ValueError("El RFC debe tener 12 o 13 caracteres.")
        return rfc

    @field_validator("clabe", check_fields=False)
    @classmethod
    def _clabe(cls, valor: str | None) -> str | None:
        if valor is None:
            return None
        clabe = valor.strip().replace(" ", "")
        # Solo largo y digitos. El digito verificador no se valida todavia:
        # implementarlo mal rechazaria CLABEs buenas y dejaria a PSD sin
        # poder dar de alta al organizador. Queda pendiente verificarlo
        # contra cuentas reales antes de activarlo.
        if len(clabe) != 18 or not clabe.isdigit():
            raise ValueError("La CLABE debe ser de 18 digitos.")
        return clabe

    @field_validator("email", check_fields=False)
    @classmethod
    def _email(cls, valor: str | None) -> str | None:
        return valor.strip().lower() if valor else valor


class ClienteCrear(_NormalizaIdentificadores):
    nombre: str = Field(min_length=2, max_length=160)
    razon_social: str | None = Field(default=None, max_length=200)
    rfc: str | None = None
    direccion: str | None = None
    contacto: str | None = Field(default=None, max_length=160)
    telefono: str | None = Field(default=None, max_length=40)
    email: str | None = Field(default=None, max_length=255)
    clabe: str | None = None

    # Si no se indican, aplican los valores por omision del modelo.
    comision_pct: Porcentaje | None = None
    cargo_servicio_pct: Porcentaje | None = None
    cargo_orden: Importe | None = None

    ga_id: str | None = Field(default=None, max_length=40)
    pixel_id: str | None = Field(default=None, max_length=40)


class ClienteActualizar(_NormalizaIdentificadores):
    nombre: str | None = Field(default=None, min_length=2, max_length=160)
    razon_social: str | None = Field(default=None, max_length=200)
    rfc: str | None = None
    direccion: str | None = None
    contacto: str | None = Field(default=None, max_length=160)
    telefono: str | None = Field(default=None, max_length=40)
    email: str | None = Field(default=None, max_length=255)
    clabe: str | None = None
    comision_pct: Porcentaje | None = None
    cargo_servicio_pct: Porcentaje | None = None
    cargo_orden: Importe | None = None
    ga_id: str | None = Field(default=None, max_length=40)
    pixel_id: str | None = Field(default=None, max_length=40)
    activo: bool | None = None


class ClienteResponse(BaseModel):
    id: int
    nombre: str
    razon_social: str | None
    rfc: str | None
    contacto: str | None
    telefono: str | None
    email: str | None
    clabe: str | None
    comision_pct: Decimal
    cargo_servicio_pct: Decimal
    cargo_orden: Decimal
    activo: bool

    model_config = {"from_attributes": True}
