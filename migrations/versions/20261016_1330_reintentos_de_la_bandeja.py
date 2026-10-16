"""Reintentos de la bandeja: no volver a intentar antes de `reintentar_en`.

El publicador de la bandeja (ADR 0015) espera cada vez mas entre un
intento fallido y el siguiente. Columna nueva y nula: no toca datos.

Revision ID: 8c41f0d2a7b3
Revises: 0720994ec244
Create Date: 2026-10-16 13:30:00-06:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "8c41f0d2a7b3"
down_revision: str | None = "0720994ec244"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "outbox_events",
        sa.Column("reintentar_en", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("outbox_events", "reintentar_en")
