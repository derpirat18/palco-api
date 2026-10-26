"""Catálogos que administra PSD: ciudades (GEO1) y categorías de eventos (CAT1).

Sin catálogo, «Saltillo», «saltillo» y «Saltillo, Coah.» eran tres ciudades en
el filtro de la cartelera, y «Circo» y «circo» dos categorías. Ahora el panel
solo deja escoger de estas listas.

- `Ciudad`: la agrega PSD (los recintos solo los da de alta PSD).
- `CategoriaDeEvento`: PSD la agrega directo; un organizador la **solicita**
  y PSD la aprueba o la rechaza. Al aprobarla se le pone al evento desde el
  que se pidió, si no tenía.

`clave` es el nombre sin acentos ni mayúsculas: impide duplicados escritos
distinto.
"""

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, PKMixin


class Ciudad(Base, PKMixin):
    __tablename__ = "cities"

    pais: Mapped[str] = mapped_column(String(2), nullable=False, default="MX")
    estado: Mapped[str] = mapped_column(String(80), nullable=False)
    nombre: Mapped[str] = mapped_column(String(80), nullable=False)
    # pais|estado|nombre, sin acentos ni mayúsculas.
    clave: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    agregada_por: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL")
    )
    agregada_en: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (Index("ix_cities_pais_estado", "pais", "estado"),)


class CategoriaDeEvento(Base, PKMixin):
    __tablename__ = "event_categories"

    nombre: Mapped[str] = mapped_column(String(60), nullable=False)
    clave: Mapped[str] = mapped_column(String(60), nullable=False, unique=True)
    # aprobada: se puede escoger. solicitada: espera a PSD. rechazada: no.
    estado: Mapped[str] = mapped_column(String(12), nullable=False)
    solicitada_por: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL")
    )
    # El evento desde el que se pidió: al aprobarla se le pone, si no tenía.
    para_evento_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("events.id", ondelete="SET NULL")
    )
    solicitada_en: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    revisada_por: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL")
    )
    revisada_en: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    motivo_rechazo: Mapped[str | None] = mapped_column(String(200))

    __table_args__ = (
        CheckConstraint(
            "estado IN ('aprobada','solicitada','rechazada')", name="estado_valido"
        ),
    )
