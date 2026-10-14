"""Inventario materializado y su bitacora.

`performance_seats` es una fila por butaca por funcion. Es la fuente de
verdad del estado del inventario y donde vive la garantia estructural de
que una butaca no se vende dos veces.
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
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, EstadoAsiento, PKMixin


class PerformanceSeat(Base, PKMixin):
    __tablename__ = "performance_seats"

    performance_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("performances.id", ondelete="CASCADE"), nullable=False
    )
    seat_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("seats.id", ondelete="RESTRICT"), nullable=False
    )
    estado: Mapped[str] = mapped_column(
        String(16), nullable=False, default=EstadoAsiento.DISPONIBLE.value
    )

    # Precio efectivo de ESTA butaca en ESTA funcion.
    precio_final: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    # True si se fijo a mano. Un cambio de precio de zona solo actualiza
    # las butacas con precio_personalizado = false.
    precio_personalizado: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )

    # Apartado vigente. Espejo del hold en Redis: Redis manda mientras
    # vive, esta columna sirve para diagnostico y para reconstruir si
    # Redis se reinicia.
    hold_id: Mapped[str | None] = mapped_column(String(40))
    hold_expira_en: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Bloqueo optimista para actualizaciones que no pasan por el hold.
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    actualizado_en: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    __table_args__ = (
        # Capa 2 de la garantia: una sola fila por butaca y funcion.
        UniqueConstraint("performance_id", "seat_id", name="performance_id_seat_id"),
        CheckConstraint(
            "estado IN ('disponible','apartado','vendido','cortesia',"
            "'bloqueado','kill')",
            name="estado_valido",
        ),
        CheckConstraint("precio_final >= 0", name="precio_no_negativo"),
        # Render del mapa: se filtra por funcion y se pinta por estado.
        Index("ix_perf_seats_performance_estado", "performance_id", "estado"),
        # Barrido de apartados vencidos.
        Index("ix_perf_seats_hold_expira", "hold_expira_en"),
    )


class InventoryMove(Base, PKMixin):
    """Bitacora de cambios de estado del inventario.

    Solo insercion. Permite reconstruir por que una butaca esta como esta,
    que es lo que resuelve las disputas en taquilla.
    """

    __tablename__ = "inventory_moves"

    performance_seat_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("performance_seats.id", ondelete="CASCADE"),
        nullable=False,
    )
    estado_previo: Mapped[str | None] = mapped_column(String(16))
    estado_nuevo: Mapped[str] = mapped_column(String(16), nullable=False)
    motivo: Mapped[str] = mapped_column(String(80), nullable=False)
    # Quien lo hizo: id de user (personal) o de customer (comprador).
    actor_tipo: Mapped[str | None] = mapped_column(String(16))
    actor_id: Mapped[int | None] = mapped_column(BigInteger)
    order_id: Mapped[int | None] = mapped_column(BigInteger)
    hold_id: Mapped[str | None] = mapped_column(String(40))

    creado_en: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        Index("ix_inv_moves_seat_creado", "performance_seat_id", "creado_en"),
    )
