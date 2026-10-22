"""Escaner en linea (E3a) y listado de ventas (RP1).

- access_assignments: a que eventos entra cada cuenta de acceso.
- scans.user_id: quien escaneo desde el panel ("ya entro con Lupita").
- data_access_log: quien vio o descargo datos de compradores.

Solo agrega (tablas nuevas y una columna nula): la version anterior del
codigo sigue funcionando mientras se despliega la nueva.

Revision ID: 7a3f9c2e1b84
Revises: 5d2e8c1a9f47
Create Date: 2026-10-22 14:00:00-06:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "7a3f9c2e1b84"
down_revision: str | None = "5d2e8c1a9f47"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "access_assignments",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("event_id", sa.BigInteger(), nullable=False),
        sa.Column("asignado_por", sa.BigInteger(), nullable=True),
        sa.Column(
            "asignado_en",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_access_assignments_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["events.id"],
            name=op.f("fk_access_assignments_event_id_events"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["asignado_por"],
            ["users.id"],
            name=op.f("fk_access_assignments_asignado_por_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_access_assignments")),
        sa.UniqueConstraint(
            "user_id", "event_id", name="uq_access_assignments_user_event"
        ),
    )
    op.create_index("ix_access_assignments_event", "access_assignments", ["event_id"])

    op.add_column("scans", sa.Column("user_id", sa.BigInteger(), nullable=True))
    op.create_foreign_key(
        op.f("fk_scans_user_id_users"),
        "scans",
        "users",
        ["user_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_scans_ticket", "scans", ["ticket_id"])

    op.create_table(
        "data_access_log",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=True),
        sa.Column("recurso", sa.String(40), nullable=False),
        sa.Column("performance_id", sa.BigInteger(), nullable=True),
        sa.Column("accion", sa.String(16), nullable=False),
        sa.Column(
            "consultado_en",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "accion IN ('ver','descargar')",
            name=op.f("ck_data_access_log_accion_valida"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_data_access_log_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["performance_id"],
            ["performances.id"],
            name=op.f("fk_data_access_log_performance_id_performances"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_data_access_log")),
    )
    op.create_index(
        "ix_data_access_log_performance",
        "data_access_log",
        ["performance_id", "consultado_en"],
    )


def downgrade() -> None:
    op.drop_index("ix_data_access_log_performance", table_name="data_access_log")
    op.drop_table("data_access_log")
    op.drop_index("ix_scans_ticket", table_name="scans")
    op.drop_constraint(op.f("fk_scans_user_id_users"), "scans", type_="foreignkey")
    op.drop_column("scans", "user_id")
    op.drop_index("ix_access_assignments_event", table_name="access_assignments")
    op.drop_table("access_assignments")
