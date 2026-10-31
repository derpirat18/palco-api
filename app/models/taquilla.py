"""Taquilla (TQ): quien vende en el lugar, sus cajas y cada venta.

- `PersonalDeTaquilla`: una cuenta del personal del organizador que vende
  (cajero) o supervisa (jefe) en un evento. PSD y el organizador hacen las
  dos cosas sin asignacion.
- `CajaDeTaquilla`: el turno de un cajero. Se abre con un fondo en efectivo
  y un rango de folios del medio fisico (pulsera, gafete). Se cierra en dos
  pasos: el cajero cuenta a ciegas y el jefe cierra viendo la diferencia.
- `VentaDeTaquilla`: lo propio de una venta en el lugar (forma de pago,
  efectivo recibido, folios entregados). La compra es una orden normal
  (`canal = 'taquilla'`, o `'cortesia'` si fue cortesia), con sus boletos.
"""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    false,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, PKMixin, UUIDPublicoMixin

CAJERO, JEFE = "cajero", "jefe"
ABIERTA, CONTADA, CERRADA = "abierta", "contada", "cerrada"
EFECTIVO, TERMINAL, CORTESIA = "efectivo", "terminal", "cortesia"


class PersonalDeTaquilla(Base, PKMixin):
    __tablename__ = "box_office_staff"

    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    event_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("events.id", ondelete="CASCADE"), nullable=False
    )
    puesto: Mapped[str] = mapped_column(String(10), nullable=False)
    asignado_por: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL")
    )
    asignado_en: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("user_id", "event_id", name="uq_box_office_staff_user_event"),
        CheckConstraint("puesto IN ('cajero','jefe')", name="puesto_valido"),
        Index("ix_box_office_staff_event", "event_id"),
    )


class CajaDeTaquilla(Base, PKMixin, UUIDPublicoMixin):
    __tablename__ = "box_office_registers"

    event_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("events.id", ondelete="RESTRICT"), nullable=False
    )
    cajero_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    abierta_por: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL")
    )
    abierta_en: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    fondo: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    # El medio fisico que se entrega con esta caja ("Pulsera amarilla") y su
    # rango de folios. Sin rango, esta caja no entrega medio fisico.
    medio: Mapped[str | None] = mapped_column(String(60))
    folio_desde: Mapped[int | None] = mapped_column(Integer)
    folio_hasta: Mapped[int | None] = mapped_column(Integer)
    estado: Mapped[str] = mapped_column(String(10), nullable=False, default=ABIERTA)

    # Lo que cuenta el cajero, sin ver lo que dice el sistema.
    contada_en: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    efectivo_contado: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    terminal_contado: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    folios_sobrantes: Mapped[int | None] = mapped_column(Integer)
    # El cierre del jefe, con lo que esperaba el sistema en ese momento.
    cerrada_en: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cerrada_por: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL")
    )
    efectivo_esperado: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    terminal_esperado: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    folios_esperados: Mapped[int | None] = mapped_column(Integer)
    nota_de_cierre: Mapped[str | None] = mapped_column(String(300))

    __table_args__ = (
        CheckConstraint(
            "estado IN ('abierta','contada','cerrada')", name="estado_valido"
        ),
        CheckConstraint("fondo >= 0", name="fondo_no_negativo"),
        CheckConstraint(
            "(folio_desde IS NULL AND folio_hasta IS NULL) OR "
            "(folio_desde IS NOT NULL AND folio_hasta IS NOT NULL "
            "AND folio_desde > 0 AND folio_hasta >= folio_desde)",
            name="rango_de_folios",
        ),
        # Un cajero, una caja sin cerrar a la vez en el evento.
        Index(
            "uq_box_office_registers_cajero_abierta",
            "event_id",
            "cajero_id",
            unique=True,
            postgresql_where=text("estado <> 'cerrada'"),
        ),
        Index("ix_box_office_registers_event", "event_id"),
    )


class VentaDeTaquilla(Base, PKMixin, UUIDPublicoMixin):
    __tablename__ = "box_office_sales"

    order_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("orders.id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    )
    caja_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("box_office_registers.id", ondelete="RESTRICT"),
        nullable=False,
    )
    vendida_por: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL")
    )
    forma_pago: Mapped[str] = mapped_column(String(10), nullable=False)
    total: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    # Efectivo: cuanto entrego el cliente y cuanto se le regreso.
    recibido: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    cambio: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    # Terminal: el numero de autorizacion del voucher.
    autorizacion: Mapped[str | None] = mapped_column(String(40))
    # Folios del medio fisico entregados (si entro en ese momento con pulsera).
    folios: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    entro_con_medio: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    # Vendida sin internet (TQ fase 3): de la reserva de la caja, con medio
    # fisico; se subio al volver la red. `vendida_en` es la hora del equipo.
    sin_red: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    # Evita cobrar dos veces si el equipo reintenta la misma venta.
    clave_cliente: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    vendida_en: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    # Correccion por error del cajero (TQ T14): solo el mismo dia, antes del
    # corte, la hace el jefe con motivo.
    anulada_en: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    anulada_por: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL")
    )
    motivo_anulacion: Mapped[str | None] = mapped_column(String(200))
    nota: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint(
            "forma_pago IN ('efectivo','terminal','cortesia')", name="forma_pago_valida"
        ),
        CheckConstraint("total >= 0", name="total_no_negativo"),
        CheckConstraint(
            "forma_pago <> 'efectivo' OR (recibido IS NOT NULL AND recibido >= total "
            "AND cambio = recibido - total)",
            name="efectivo_cuadra",
        ),
        CheckConstraint(
            "forma_pago <> 'terminal' OR autorizacion IS NOT NULL",
            name="terminal_con_autorizacion",
        ),
        Index("ix_box_office_sales_caja", "caja_id"),
    )
