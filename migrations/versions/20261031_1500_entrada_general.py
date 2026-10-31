"""Entrada general (EG): zonas sin lugar numerado.

Una zona general es un cupo: sus lugares (fila G, numero 1..N) los asigna
el sistema y el boleto dice "Acceso general". Asi la venta usa el mismo
inventario, apartado y cobro que las butacas. Solo agrega.

Revision ID: 9d5a3b7f1c28
Revises: 8c4f2a6e9b17
Create Date: 2026-10-31 15:00:00-06:00
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "9d5a3b7f1c28"
down_revision: str | None = "8c4f2a6e9b17"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "zones",
        sa.Column("general", sa.Boolean(), server_default=sa.false(), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("zones", "general")
