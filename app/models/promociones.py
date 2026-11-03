"""Promociones (PR1): 2x1 o un porcentaje, por dias de la semana.

Cada organizador programa las suyas como una alarma: los dias (de la
funcion, no de la compra), en linea, en taquilla o en las dos, en todos sus
eventos o en algunos eventos y zonas. Se encienden y se apagan. PSD puede
editar todas y es el unico que pone una comision especial.

Lo que se vendio no depende de esta tabla: la orden congela el nombre de la
promocion y cada boleto su descuento y su comision.
"""

from datetime import date
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Numeric,
    SmallInteger,
    String,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, PKMixin, TimestampMixin

DOS_POR_UNO = "2x1"
PORCENTAJE = "porcentaje"
TIPOS_DE_PROMOCION = (DOS_POR_UNO, PORCENTAJE)
# Un descuento de 100 % es una cortesia, y las cortesias van por otro camino.
PORCENTAJE_MAXIMO = Decimal("90")


class Promocion(Base, PKMixin, TimestampMixin):
    __tablename__ = "promociones"

    client_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("clients.id", ondelete="CASCADE"), nullable=False
    )
    # Lo ve el comprador junto a la fecha ("2x1 de martes").
    nombre: Mapped[str] = mapped_column(String(60), nullable=False)
    tipo: Mapped[str] = mapped_column(String(12), nullable=False)
    porcentaje: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    # Dias de la funcion, a la hora del recinto: bit 0 = lunes ... bit 6 =
    # domingo (como `date.weekday()`).
    dias: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    # Apagado: solo el boleto de adulto. Encendido: tambien nino y adulto mayor.
    todos_los_tipos: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    en_linea: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    en_taquilla: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # "Solo entre fechas": la fecha de la funcion, a la hora del recinto.
    desde: Mapped[date | None] = mapped_column(Date)
    hasta: Mapped[date | None] = mapped_column(Date)
    # Encendido: todos los eventos del organizador, tambien los que cree
    # despues. Apagado: los de `promocion_eventos`.
    todos_los_eventos: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )
    # Solo PSD. Vacio: la comision de siempre (en linea la del organizador,
    # en taquilla la de la taquilla). Se cobra sobre lo que paga el comprador.
    comision_pct: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    activa: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    creada_por: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL")
    )
    actualizada_por: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL")
    )

    __table_args__ = (
        CheckConstraint("tipo IN ('2x1','porcentaje')", name="tipo_valido"),
        CheckConstraint(
            "(tipo = '2x1' AND porcentaje IS NULL) OR "
            "(tipo = 'porcentaje' AND porcentaje >= 1 AND porcentaje <= 90)",
            name="porcentaje_valido",
        ),
        CheckConstraint("dias BETWEEN 1 AND 127", name="dias_validos"),
        CheckConstraint("en_linea OR en_taquilla", name="algun_canal"),
        CheckConstraint(
            "desde IS NULL OR hasta IS NULL OR desde <= hasta", name="fechas_en_orden"
        ),
        CheckConstraint(
            "comision_pct IS NULL OR comision_pct BETWEEN 0 AND 100",
            name="comision_rango",
        ),
        Index("ix_promociones_cliente", "client_id"),
    )


class PromocionEvento(Base, PKMixin):
    """Un evento (y opcionalmente una zona) donde aplica la promocion.

    `zone_id` nulo: todas las zonas del evento.
    """

    __tablename__ = "promocion_eventos"

    promocion_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("promociones.id", ondelete="CASCADE"), nullable=False
    )
    event_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("events.id", ondelete="CASCADE"), nullable=False
    )
    zone_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("zones.id", ondelete="CASCADE")
    )

    __table_args__ = (
        Index("ix_promocion_eventos_promocion", "promocion_id"),
        Index("ix_promocion_eventos_evento", "event_id"),
    )
