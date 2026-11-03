"""Promociones (PR1): 2x1 o porcentaje por dias de la funcion.

- `promociones` y `promocion_eventos`: las programa cada organizador.
- `orders.promocion_id` / `promocion_snap`: la que se aplico y su nombre.
- `order_items.comision_pct_snap`: la comision especial de PSD en los
  boletos de la promocion (nula: la de la orden).

Solo agrega tablas y columnas nulas: el codigo anterior sigue funcionando.

Revision ID: d8e2f4a6b1c3
Revises: c3f8a1d5e2b9
Create Date: 2026-11-03 00:30:00-06:00
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "d8e2f4a6b1c3"
down_revision: str | None = "c3f8a1d5e2b9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "promociones",
        sa.Column("client_id", sa.BigInteger(), nullable=False),
        sa.Column("nombre", sa.String(length=60), nullable=False),
        sa.Column("tipo", sa.String(length=12), nullable=False),
        sa.Column("porcentaje", sa.Numeric(precision=5, scale=2), nullable=True),
        sa.Column("dias", sa.SmallInteger(), nullable=False),
        sa.Column(
            "todos_los_tipos", sa.Boolean(), server_default="false", nullable=False
        ),
        sa.Column("en_linea", sa.Boolean(), nullable=False),
        sa.Column("en_taquilla", sa.Boolean(), nullable=False),
        sa.Column("desde", sa.Date(), nullable=True),
        sa.Column("hasta", sa.Date(), nullable=True),
        sa.Column("todos_los_eventos", sa.Boolean(), nullable=False),
        sa.Column("comision_pct", sa.Numeric(precision=5, scale=2), nullable=True),
        sa.Column("activa", sa.Boolean(), nullable=False),
        sa.Column("creada_por", sa.BigInteger(), nullable=True),
        sa.Column("actualizada_por", sa.BigInteger(), nullable=True),
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column(
            "creado_en",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "actualizado_en",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(tipo = '2x1' AND porcentaje IS NULL) OR (tipo = 'porcentaje' AND porcentaje >= 1 AND porcentaje <= 90)",
            name=op.f("ck_promociones_porcentaje_valido"),
        ),
        sa.CheckConstraint(
            "tipo IN ('2x1','porcentaje')", name=op.f("ck_promociones_tipo_valido")
        ),
        sa.CheckConstraint(
            "comision_pct IS NULL OR comision_pct BETWEEN 0 AND 100",
            name=op.f("ck_promociones_comision_rango"),
        ),
        sa.CheckConstraint(
            "desde IS NULL OR hasta IS NULL OR desde <= hasta",
            name=op.f("ck_promociones_fechas_en_orden"),
        ),
        sa.CheckConstraint(
            "dias BETWEEN 1 AND 127", name=op.f("ck_promociones_dias_validos")
        ),
        sa.CheckConstraint(
            "en_linea OR en_taquilla", name=op.f("ck_promociones_algun_canal")
        ),
        sa.ForeignKeyConstraint(
            ["actualizada_por"],
            ["users.id"],
            name=op.f("fk_promociones_actualizada_por_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["client_id"],
            ["clients.id"],
            name=op.f("fk_promociones_client_id_clients"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["creada_por"],
            ["users.id"],
            name=op.f("fk_promociones_creada_por_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_promociones")),
    )
    op.create_index(
        "ix_promociones_cliente", "promociones", ["client_id"], unique=False
    )
    op.create_table(
        "promocion_eventos",
        sa.Column("promocion_id", sa.BigInteger(), nullable=False),
        sa.Column("event_id", sa.BigInteger(), nullable=False),
        sa.Column("zone_id", sa.BigInteger(), nullable=True),
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["events.id"],
            name=op.f("fk_promocion_eventos_event_id_events"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["promocion_id"],
            ["promociones.id"],
            name=op.f("fk_promocion_eventos_promocion_id_promociones"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["zone_id"],
            ["zones.id"],
            name=op.f("fk_promocion_eventos_zone_id_zones"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_promocion_eventos")),
    )
    op.create_index(
        "ix_promocion_eventos_evento", "promocion_eventos", ["event_id"], unique=False
    )
    op.create_index(
        "ix_promocion_eventos_promocion",
        "promocion_eventos",
        ["promocion_id"],
        unique=False,
    )
    op.add_column(
        "order_items",
        sa.Column("comision_pct_snap", sa.Numeric(precision=5, scale=2), nullable=True),
    )
    op.add_column("orders", sa.Column("promocion_id", sa.BigInteger(), nullable=True))
    op.add_column(
        "orders", sa.Column("promocion_snap", sa.String(length=60), nullable=True)
    )
    op.create_foreign_key(
        op.f("fk_orders_promocion_id_promociones"),
        "orders",
        "promociones",
        ["promocion_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_orders_promocion",
        "orders",
        ["promocion_id"],
        unique=False,
        postgresql_where=sa.text("promocion_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_orders_promocion", table_name="orders")
    op.drop_constraint(
        op.f("fk_orders_promocion_id_promociones"), "orders", type_="foreignkey"
    )
    op.drop_column("orders", "promocion_snap")
    op.drop_column("orders", "promocion_id")
    op.drop_column("order_items", "comision_pct_snap")
    op.drop_index("ix_promocion_eventos_promocion", table_name="promocion_eventos")
    op.drop_index("ix_promocion_eventos_evento", table_name="promocion_eventos")
    op.drop_table("promocion_eventos")
    op.drop_index("ix_promociones_cliente", table_name="promociones")
    op.drop_table("promociones")
