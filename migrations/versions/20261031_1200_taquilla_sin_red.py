"""Taquilla sin internet (TQ fase 3): ventas que se subieron al volver la red.

La reserva de cada caja no necesita tabla: son butacas apartadas con el
apartado `caja-<uuid>` que vence cuando cierra la venta en linea. Aqui solo
se marca la venta que se hizo sin red. Solo agrega.

Revision ID: 8c4f2a6e9b17
Revises: 7b3e51c0d8a4
Create Date: 2026-10-31 12:00:00-06:00
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "8c4f2a6e9b17"
down_revision: str | None = "7b3e51c0d8a4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "box_office_sales",
        sa.Column("sin_red", sa.Boolean(), server_default=sa.false(), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("box_office_sales", "sin_red")
