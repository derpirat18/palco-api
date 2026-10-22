"""Nombre por compra: a nombre de quien se hizo cada orden.

El comprador es un registro por correo y no se cambia si ya existia (un
correo ajeno no le cambia los datos a su dueno). Pero la lista de la
puerta y el correo de los boletos deben llevar el nombre que se escribio
en esa compra. Columnas nuevas y nulas: no tocan datos y la version
anterior del codigo sigue funcionando con ellas.

Revision ID: 5d2e8c1a9f47
Revises: bafe65d31d0f
Create Date: 2026-10-21 19:45:00-06:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "5d2e8c1a9f47"
down_revision: str | None = "bafe65d31d0f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("orders", sa.Column("nombre_snap", sa.String(120), nullable=True))
    op.add_column("orders", sa.Column("apellidos_snap", sa.String(120), nullable=True))


def downgrade() -> None:
    op.drop_column("orders", "apellidos_snap")
    op.drop_column("orders", "nombre_snap")
