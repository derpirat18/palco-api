"""Devolucion al corregir una venta de taquilla (3 nov).

Cuando el jefe corrige una venta por error del cajero, confirma con el
cliente enfrente como le devolvio su dinero:

- `box_office_sales.devolucion`: 'efectivo' o 'terminal' (en una cortesia
  no hay dinero y queda nula).
- `box_office_sales.devuelto`: cuanto se devolvio (el total de la venta).

Solo agrega columnas nulas: el codigo anterior sigue funcionando.

Revision ID: e5b9c3d7a2f4
Revises: d8e2f4a6b1c3
Create Date: 2026-11-03 12:00:00-06:00
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "e5b9c3d7a2f4"
down_revision: str | None = "d8e2f4a6b1c3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "box_office_sales",
        sa.Column("devolucion", sa.String(length=10), nullable=True),
    )
    op.add_column(
        "box_office_sales",
        sa.Column("devuelto", sa.Numeric(precision=12, scale=2), nullable=True),
    )
    op.create_check_constraint(
        op.f("ck_box_office_sales_devolucion_valida"),
        "box_office_sales",
        "devolucion IS NULL OR devolucion IN ('efectivo','terminal')",
    )
    op.create_check_constraint(
        op.f("ck_box_office_sales_devuelto_no_negativo"),
        "box_office_sales",
        "devuelto IS NULL OR devuelto >= 0",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_box_office_sales_devuelto_no_negativo"),
        "box_office_sales",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_box_office_sales_devolucion_valida"),
        "box_office_sales",
        type_="check",
    )
    op.drop_column("box_office_sales", "devuelto")
    op.drop_column("box_office_sales", "devolucion")
