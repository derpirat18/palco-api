"""Cuentas de comprador (R1): numero de sesion para poder cerrarlas todas.

El comprador entra con un codigo que le llega a su correo; el token de su
sesion lleva este numero. Subirlo cierra sus sesiones en todos sus
equipos. Solo agrega una columna con valor por omision.

Revision ID: 5c1e9a7d3b20
Revises: 2b6e8d4f1c73
Create Date: 2026-10-27 09:00:00-06:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "5c1e9a7d3b20"
down_revision: str | None = "2b6e8d4f1c73"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "customers",
        sa.Column(
            "sesion_version", sa.Integer(), nullable=False, server_default="0"
        ),
    )


def downgrade() -> None:
    op.drop_column("customers", "sesion_version")
