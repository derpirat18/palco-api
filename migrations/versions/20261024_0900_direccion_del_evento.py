"""Direccion personalizada del evento (URL1): las direcciones anteriores.

Cuando la direccion de un evento cambia, la vieja se guarda aqui para que
las ligas que ya circulan (anuncios, mensajes) sigan llevando al evento.
Solo agrega una tabla: el codigo anterior no la usa y sigue funcionando.

Revision ID: 5e1a8c3b7d90
Revises: 9b4d2e7f1a63
Create Date: 2026-10-24 09:00:00-06:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "5e1a8c3b7d90"
down_revision: str | None = "9b4d2e7f1a63"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "event_slug_history",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("event_id", sa.BigInteger(), nullable=False),
        sa.Column("slug", sa.String(160), nullable=False),
        sa.Column("cambiado_por", sa.BigInteger(), nullable=True),
        sa.Column(
            "cambiado_en",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["events.id"],
            name=op.f("fk_event_slug_history_event_id_events"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["cambiado_por"],
            ["users.id"],
            name=op.f("fk_event_slug_history_cambiado_por_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_event_slug_history")),
        sa.UniqueConstraint("slug", name=op.f("uq_event_slug_history_slug")),
    )
    op.create_index("ix_event_slug_history_event", "event_slug_history", ["event_id"])


def downgrade() -> None:
    op.drop_index("ix_event_slug_history_event", table_name="event_slug_history")
    op.drop_table("event_slug_history")
