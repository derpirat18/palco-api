"""Boletos B: la tarjeta de la compra (BOL1) y las alertas por correo (A1, A4).

- `orders.tarjeta_snap`: "Visa •••• 4242", para el correo. Nula en las
  compras de antes: el correo dice solo "Pagado el ...".
- `alert_recipients`: a quien le llegan las alertas. Se administra en el
  panel; arranca con JuanMa y Nadia (25 oct).
- `alerts`: bitacora de lo que se aviso.

Solo agrega: el codigo anterior no usa nada de esto y sigue funcionando.

Revision ID: 7d3f9a2c4e81
Revises: 5e1a8c3b7d90
Create Date: 2026-10-25 17:00:00-06:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "7d3f9a2c4e81"
down_revision: str | None = "5e1a8c3b7d90"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DESTINATARIOS_INICIALES = [
    ("juanmanuel@psdigital.mx", "JuanMa"),
    ("nadia@psdigital.mx", "Nadia"),
]


def upgrade() -> None:
    op.add_column("orders", sa.Column("tarjeta_snap", sa.String(40), nullable=True))

    destinatarios = op.create_table(
        "alert_recipients",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("nombre", sa.String(120), nullable=True),
        sa.Column("agregado_por", sa.BigInteger(), nullable=True),
        sa.Column(
            "agregado_en",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["agregado_por"],
            ["users.id"],
            name=op.f("fk_alert_recipients_agregado_por_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_alert_recipients")),
        sa.UniqueConstraint("email", name=op.f("uq_alert_recipients_email")),
    )
    op.bulk_insert(
        destinatarios,
        [{"email": e, "nombre": n} for e, n in DESTINATARIOS_INICIALES],
    )

    op.create_table(
        "alerts",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("clave", sa.String(200), nullable=False),
        sa.Column("titulo", sa.String(200), nullable=False),
        sa.Column("detalle", sa.Text(), nullable=False),
        sa.Column("enviada_a", sa.Integer(), nullable=False),
        sa.Column("prueba", sa.Boolean(), nullable=False),
        sa.Column(
            "creada_en",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_alerts")),
    )
    op.create_index("ix_alerts_clave_creada", "alerts", ["clave", "creada_en"])


def downgrade() -> None:
    op.drop_index("ix_alerts_clave_creada", table_name="alerts")
    op.drop_table("alerts")
    op.drop_table("alert_recipients")
    op.drop_column("orders", "tarjeta_snap")
