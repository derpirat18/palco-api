"""Deshacer una entrada (E3d) y listado de ventas por evento (RP2).

- scans.resultado admite 'deshecho': la constancia de quien regreso a
  valido un boleto escaneado por error.
- data_access_log.event_id: la descarga de todo un evento queda en la
  bitacora con su evento.

Solo amplia: la version anterior del codigo nunca escribe 'deshecho' ni
event_id, asi que sigue funcionando mientras se despliega la nueva.

Revision ID: 3c8e1f6a9d25
Revises: 7a3f9c2e1b84
Create Date: 2026-10-23 10:00:00-06:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "3c8e1f6a9d25"
down_revision: str | None = "7a3f9c2e1b84"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ANTES = "resultado IN ('valido','duplicado','otra_funcion','invalido','cancelado')"
AHORA = (
    "resultado IN ('valido','duplicado','otra_funcion','invalido',"
    "'cancelado','deshecho')"
)


def upgrade() -> None:
    op.drop_constraint(op.f("ck_scans_resultado_valido"), "scans", type_="check")
    op.create_check_constraint(op.f("ck_scans_resultado_valido"), "scans", AHORA)

    op.add_column(
        "data_access_log", sa.Column("event_id", sa.BigInteger(), nullable=True)
    )
    op.create_foreign_key(
        op.f("fk_data_access_log_event_id_events"),
        "data_access_log",
        "events",
        ["event_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("fk_data_access_log_event_id_events"),
        "data_access_log",
        type_="foreignkey",
    )
    op.drop_column("data_access_log", "event_id")
    # Las constancias de entradas deshechas no caben en la regla anterior.
    op.execute("DELETE FROM scans WHERE resultado = 'deshecho'")
    op.drop_constraint(op.f("ck_scans_resultado_valido"), "scans", type_="check")
    op.create_check_constraint(op.f("ck_scans_resultado_valido"), "scans", ANTES)
