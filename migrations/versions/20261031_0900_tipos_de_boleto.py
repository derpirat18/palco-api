"""Tipos de boleto (TB): adulto, nino y adulto mayor.

El evento anuncia las edades; cada zona de cada funcion puede tener precio
de nino y de adulto mayor (sin precio, ese tipo no se vende ahi); cada
partida guarda su tipo. Solo agrega: las partidas que ya existen quedan
como adulto y la venta sin tipos funciona igual que antes.

Revision ID: 7b3e51c0d8a4
Revises: 59d9af224dae
Create Date: 2026-10-31 09:00:00-06:00
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "7b3e51c0d8a4"
down_revision: str | None = "59d9af224dae"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("events", sa.Column("edad_nino_hasta", sa.Integer(), nullable=True))
    op.add_column("events", sa.Column("edad_mayor_desde", sa.Integer(), nullable=True))
    op.create_check_constraint(
        op.f("ck_events_edad_nino_rango"),
        "events",
        "edad_nino_hasta IS NULL OR edad_nino_hasta BETWEEN 1 AND 17",
    )
    op.create_check_constraint(
        op.f("ck_events_edad_mayor_rango"),
        "events",
        "edad_mayor_desde IS NULL OR edad_mayor_desde BETWEEN 50 AND 99",
    )

    op.add_column(
        "performance_prices",
        sa.Column("precio_nino", sa.Numeric(precision=10, scale=2), nullable=True),
    )
    op.add_column(
        "performance_prices",
        sa.Column("precio_mayor", sa.Numeric(precision=10, scale=2), nullable=True),
    )
    op.create_check_constraint(
        op.f("ck_performance_prices_precio_nino_no_negativo"),
        "performance_prices",
        "precio_nino IS NULL OR precio_nino >= 0",
    )
    op.create_check_constraint(
        op.f("ck_performance_prices_precio_mayor_no_negativo"),
        "performance_prices",
        "precio_mayor IS NULL OR precio_mayor >= 0",
    )

    # Con server_default, Postgres llena las filas que ya existen sin
    # reescribir la tabla.
    op.add_column(
        "order_items",
        sa.Column(
            "tipo", sa.String(length=12), server_default="adulto", nullable=False
        ),
    )
    op.create_check_constraint(
        op.f("ck_order_items_tipo_valido"),
        "order_items",
        "tipo IN ('adulto','nino','mayor')",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_order_items_tipo_valido"), "order_items", type_="check"
    )
    op.drop_column("order_items", "tipo")
    op.drop_constraint(
        op.f("ck_performance_prices_precio_mayor_no_negativo"),
        "performance_prices",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_performance_prices_precio_nino_no_negativo"),
        "performance_prices",
        type_="check",
    )
    op.drop_column("performance_prices", "precio_mayor")
    op.drop_column("performance_prices", "precio_nino")
    op.drop_constraint(op.f("ck_events_edad_mayor_rango"), "events", type_="check")
    op.drop_constraint(op.f("ck_events_edad_nino_rango"), "events", type_="check")
    op.drop_column("events", "edad_mayor_desde")
    op.drop_column("events", "edad_nino_hasta")
