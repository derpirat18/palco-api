"""Descuentos y liquidaciones a organizadores."""

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import (
    Base,
    EstadoLiquidacion,
    PKMixin,
    TimestampMixin,
    TipoDescuento,
)


class DiscountCode(Base, PKMixin, TimestampMixin):
    __tablename__ = "discount_codes"

    client_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("clients.id", ondelete="CASCADE"), nullable=False
    )
    # Nulo = aplica a todos los eventos del organizador.
    event_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("events.id", ondelete="CASCADE")
    )
    codigo: Mapped[str] = mapped_column(String(40), nullable=False)
    tipo: Mapped[str] = mapped_column(
        String(12), nullable=False, default=TipoDescuento.PORCENTAJE.value
    )
    valor: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    usos_maximos: Mapped[int | None] = mapped_column(Integer)
    usos_actuales: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    limite_por_cliente: Mapped[int | None] = mapped_column(Integer)
    vigente_desde: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    vigente_hasta: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    activo: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    __table_args__ = (
        UniqueConstraint("client_id", "codigo", name="client_id_codigo"),
        CheckConstraint("tipo IN ('porcentaje','monto')", name="tipo_valido"),
        CheckConstraint("valor >= 0", name="valor_no_negativo"),
    )


class DiscountRedemption(Base, PKMixin):
    __tablename__ = "discount_redemptions"

    discount_code_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("discount_codes.id", ondelete="RESTRICT"), nullable=False
    )
    order_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("orders.id", ondelete="CASCADE"), nullable=False
    )
    customer_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("customers.id", ondelete="RESTRICT"), nullable=False
    )
    monto: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    creado_en: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("discount_code_id", "order_id", name="codigo_orden"),
        Index("ix_redemptions_cliente", "discount_code_id", "customer_id"),
    )


class Settlement(Base, PKMixin, TimestampMixin):
    """Liquidacion a un organizador por un periodo.

    Los importes se suman de los valores congelados en cada orden, nunca
    se recalculan desde la configuracion vigente del cliente.
    """

    __tablename__ = "settlements"

    client_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("clients.id", ondelete="RESTRICT"), nullable=False
    )
    periodo_inicio: Mapped[date] = mapped_column(Date, nullable=False)
    periodo_fin: Mapped[date] = mapped_column(Date, nullable=False)
    bruto: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, default=0)
    comision_psd: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), nullable=False, default=0
    )
    comision_stripe: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), nullable=False, default=0
    )
    reembolsos: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), nullable=False, default=0
    )
    neto: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, default=0)
    estado: Mapped[str] = mapped_column(
        String(16), nullable=False, default=EstadoLiquidacion.BORRADOR.value
    )
    facturado: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    pagada_en: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    referencia_pago: Mapped[str | None] = mapped_column(String(80))

    __table_args__ = (
        CheckConstraint(
            "estado IN ('borrador','por_pagar','pagada')", name="estado_valido"
        ),
        CheckConstraint("periodo_fin >= periodo_inicio", name="periodo_coherente"),
        Index("ix_settlements_cliente_periodo", "client_id", "periodo_inicio"),
    )


class SettlementItem(Base, PKMixin):
    __tablename__ = "settlement_items"

    settlement_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("settlements.id", ondelete="CASCADE"), nullable=False
    )
    order_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("orders.id", ondelete="RESTRICT"), nullable=False
    )
    bruto: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    comision_psd: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    comision_stripe: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    neto: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)

    __table_args__ = (
        UniqueConstraint("settlement_id", "order_id", name="settlement_orden"),
    )
