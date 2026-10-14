"""Bitacora de eventos de Stripe."""

from datetime import datetime

from sqlalchemy import DateTime, Index, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, PKMixin


class PaymentEvent(Base, PKMixin):
    """Webhook recibido de Stripe.

    `stripe_event_id` unico es lo que hace idempotente el procesamiento:
    Stripe reintenta, y el segundo intento no vuelve a emitir boletos.
    """

    __tablename__ = "payment_events"

    stripe_event_id: Mapped[str] = mapped_column(
        String(80), nullable=False, unique=True
    )
    tipo: Mapped[str] = mapped_column(String(80), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    procesado_en: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)
    recibido_en: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (Index("ix_payment_events_tipo", "tipo", "recibido_en"),)
