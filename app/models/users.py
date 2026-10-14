"""Personal: admin de PSD, organizadores y accesos.

Separado de `customers` a proposito: distinto ciclo de vida, distinto
volumen y distintas necesidades de autenticacion. Un comprador nunca
puede escalar a personal porque son tablas distintas.
"""

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    String,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, PKMixin, TimestampMixin


class User(Base, PKMixin, TimestampMixin):
    __tablename__ = "users"

    # Nulo solo para admin_psd: ese rol no pertenece a ningun organizador.
    client_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("clients.id", ondelete="CASCADE")
    )
    email: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    nombre: Mapped[str] = mapped_column(String(120), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    rol: Mapped[str] = mapped_column(String(20), nullable=False)
    mfa_secret: Mapped[str | None] = mapped_column(String(64))
    activo: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    ultimo_acceso: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "rol IN ('admin_psd','organizador','acceso')", name="rol_valido"
        ),
        CheckConstraint(
            "(rol = 'admin_psd' AND client_id IS NULL) OR "
            "(rol <> 'admin_psd' AND client_id IS NOT NULL)",
            name="client_id_segun_rol",
        ),
    )
