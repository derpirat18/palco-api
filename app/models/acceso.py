"""Quien trabaja en que evento, y quien consulto datos de compradores.

Las cuentas de acceso son de un organizador, pero no ven todos sus
eventos: PSD asigna cada cuenta a los eventos donde va a escanear (E3a).
Asi una persona contratada para Puebla no abre la lista de Tuxtla.

`ConsultaDeDatos` es la bitacora de quien vio o descargo datos personales
de compradores (correo, telefono): el listado de ventas los trae, y el
aviso de privacidad obliga a poder decir quien tuvo acceso y cuando.
"""

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, PKMixin


class AsignacionDeAcceso(Base, PKMixin):
    """Una cuenta de acceso puede escanear en un evento (todas sus funciones)."""

    __tablename__ = "access_assignments"

    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    event_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("events.id", ondelete="CASCADE"), nullable=False
    )
    asignado_por: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL")
    )
    asignado_en: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "user_id", "event_id", name="uq_access_assignments_user_event"
        ),
        Index("ix_access_assignments_event", "event_id"),
    )


class ConsultaDeDatos(Base, PKMixin):
    """Alguien del personal vio o descargo datos de compradores. Solo se inserta."""

    __tablename__ = "data_access_log"

    user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL")
    )
    # Que se consulto: el listado de ventas de una funcion o de un evento.
    recurso: Mapped[str] = mapped_column(String(40), nullable=False)
    performance_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("performances.id", ondelete="SET NULL")
    )
    # El listado de todo un evento (RP2) no es de una funcion: va aqui.
    event_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("events.id", ondelete="SET NULL")
    )
    # ver: en pantalla. descargar: a Excel (sale del sistema).
    accion: Mapped[str] = mapped_column(String(16), nullable=False)
    consultado_en: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        CheckConstraint("accion IN ('ver','descargar')", name="accion_valida"),
        Index("ix_data_access_log_performance", "performance_id", "consultado_en"),
    )
