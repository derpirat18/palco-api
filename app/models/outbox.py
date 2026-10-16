"""Bandeja de salida: hechos del negocio para otros sistemas.

Cuando una orden se paga, el CRM, la mensajeria y la analitica tienen que
enterarse. Avisarles directo desde el checkout tiene dos fallas: si el
aviso sale y la transaccion se revierte, anunciamos una venta que no
existe; si la transaccion confirma y el aviso falla, la perdemos.

Por eso el hecho se escribe aqui, en la misma transaccion que lo produce.
Un publicador aparte lo entrega despues y marca `publicado_en`; si falla,
reintenta. Nada se pierde y nada se anuncia de mas.

El `payload` lleva identificadores publicos (uuid) e importes, no datos
personales: quien lo consume pide el detalle con sus propios permisos.
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime, Index, Integer, String, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, PKMixin, UUIDPublicoMixin


class OutboxEvent(Base, PKMixin, UUIDPublicoMixin):
    __tablename__ = "outbox_events"

    # orden.pagada, orden.cancelada, ...
    tipo: Mapped[str] = mapped_column(String(60), nullable=False)
    # A que se refiere el hecho, por su uuid publico.
    agregado_uuid: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    creado_en: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    publicado_en: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    intentos: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    ultimo_error: Mapped[str | None] = mapped_column(Text)
    # Tras un fallo, no antes de esta hora (espera creciente, ADR 0015).
    reintentar_en: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        # Lo que el publicador busca: lo pendiente, en orden.
        Index(
            "ix_outbox_pendientes",
            "creado_en",
            postgresql_where=text("publicado_en IS NULL"),
        ),
        Index("ix_outbox_agregado", "agregado_uuid", "creado_en"),
    )
