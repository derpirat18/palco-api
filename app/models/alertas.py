"""Alertas para PSD por correo (A1, A4).

`DestinatarioDeAlertas`: a quien le llegan. Se administran desde el panel
(solo PSD), sin desplegar nada.

`Alerta`: bitacora de lo que se aviso. Sirve tambien para no repetir: la
misma `clave` no vuelve a salir antes de la espera (app/services/alertas.py).
"""

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, PKMixin


class DestinatarioDeAlertas(Base, PKMixin):
    __tablename__ = "alert_recipients"

    # Normalizado en minusculas.
    email: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    nombre: Mapped[str | None] = mapped_column(String(120))
    agregado_por: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL")
    )
    agregado_en: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Alerta(Base, PKMixin):
    __tablename__ = "alerts"

    # Que paso, para no repetirlo: "latido:bandeja", "error:POST /x:KeyError".
    clave: Mapped[str] = mapped_column(String(200), nullable=False)
    titulo: Mapped[str] = mapped_column(String(200), nullable=False)
    detalle: Mapped[str] = mapped_column(Text, nullable=False)
    # A cuantos destinatarios salio. 0: no habia a quien o el correo fallo.
    enviada_a: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Una prueba desde el panel no cuenta como incidente.
    prueba: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    creada_en: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (Index("ix_alerts_clave_creada", "clave", "creada_en"),)
