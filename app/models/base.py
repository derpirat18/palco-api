"""Base declarativa, mixins y enumeraciones del dominio."""

import enum
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, MetaData, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Convencion de nombres para que Alembic genere migraciones estables.
# Sin esto, los nombres de constraints los inventa Postgres y los diffs
# de Alembic se vuelven ruidosos entre maquinas.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class PKMixin:
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)


class TimestampMixin:
    creado_en: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    actualizado_en: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


# --------------------------------------------------------------------------
# Enumeraciones
# --------------------------------------------------------------------------
# Se guardan como texto con CHECK constraint, no como tipo ENUM de Postgres.
# Agregar un valor a un ENUM nativo requiere ALTER TYPE y bloquea; con texto
# mas CHECK es una migracion ordinaria.


class EstadoAsiento(enum.StrEnum):
    """Los seis estados del inventario por funcion."""

    DISPONIBLE = "disponible"
    APARTADO = "apartado"
    VENDIDO = "vendido"
    CORTESIA = "cortesia"
    BLOQUEADO = "bloqueado"  # produccion, prensa, patrocinadores
    KILL = "kill"  # butaca inutilizable (visibilidad, sonido)


class EstadoEvento(enum.StrEnum):
    BORRADOR = "borrador"
    PUBLICADO = "publicado"
    ARCHIVADO = "archivado"


class EstadoFuncion(enum.StrEnum):
    PROGRAMADA = "programada"
    EN_VENTA = "en_venta"
    AGOTADA = "agotada"
    CANCELADA = "cancelada"
    REALIZADA = "realizada"


class EstadoOrden(enum.StrEnum):
    PENDIENTE = "pendiente"
    PAGADA = "pagada"
    CANCELADA = "cancelada"
    REEMBOLSADA = "reembolsada"
    EXPIRADA = "expirada"


class EstadoBoleto(enum.StrEnum):
    VALIDO = "valido"
    TRANSFERIDO = "transferido"
    USADO = "usado"
    CANCELADO = "cancelado"


class EstadoTransferencia(enum.StrEnum):
    PENDIENTE = "pendiente"
    ACEPTADA = "aceptada"
    CANCELADA = "cancelada"
    EXPIRADA = "expirada"


class ResultadoEscaneo(enum.StrEnum):
    VALIDO = "valido"
    DUPLICADO = "duplicado"
    OTRA_FUNCION = "otra_funcion"
    INVALIDO = "invalido"
    CANCELADO = "cancelado"


class TipoDescuento(enum.StrEnum):
    PORCENTAJE = "porcentaje"
    MONTO = "monto"


class EstadoLiquidacion(enum.StrEnum):
    BORRADOR = "borrador"
    POR_PAGAR = "por_pagar"
    PAGADA = "pagada"


class RolUsuario(enum.StrEnum):
    """Roles del personal. Los compradores viven en `customers`."""

    ADMIN_PSD = "admin_psd"
    ORGANIZADOR = "organizador"
    ACCESO = "acceso"
