"""Reportes (RP3): la bitacora guarda con que filtros y formato.

Ver o descargar Compradores, descargar cualquier reporte y buscar correos
quedan en `data_access_log` con sus filtros. Solo agrega una columna.

Revision ID: c3f8a1d5e2b9
Revises: b7d2e9a4c6f1
Create Date: 2026-11-02 23:30:00-06:00
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "c3f8a1d5e2b9"
down_revision: str | None = "b7d2e9a4c6f1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "data_access_log",
        sa.Column("filtros", postgresql.JSONB(astext_type=sa.Text())),
    )


def downgrade() -> None:
    op.drop_column("data_access_log", "filtros")
