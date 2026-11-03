"""Configuracion de la taquilla: lo que tenia duda queda editable.

Por evento: comision de PSD sobre la taquilla, quien se queda el efectivo,
de quien es la terminal (estos tres solo PSD), pie del recibo, formas de
pago, lugares por venta, reserva para vender sin internet (cuantos y de que
zona) y la diferencia que se acepta en el corte sin nota. Cada venta guarda
quien se queda el efectivo y de quien es la terminal. Solo agrega: los
valores de inicio son los de siempre.

Revision ID: a1e6c4d9f0b3
Revises: 9d5a3b7f1c28
Create Date: 2026-11-02 18:00:00-06:00
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "a1e6c4d9f0b3"
down_revision: str | None = "9d5a3b7f1c28"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CHECKS_EVENTO = {
    "taquilla_comision_rango": (
        "taquilla_comision_pct IS NULL OR taquilla_comision_pct BETWEEN 0 AND 100"
    ),
    "taquilla_efectivo_valido": "taquilla_efectivo_lo_tiene IN ('psd','organizador')",
    "taquilla_terminal_valida": "taquilla_terminal_de IN ('psd','organizador')",
    "taquilla_max_lugares_rango": "taquilla_max_lugares BETWEEN 1 AND 50",
    "taquilla_reserva_rango": "taquilla_reserva_cantidad BETWEEN 1 AND 50",
    "taquilla_tolerancia_no_negativa": "taquilla_tolerancia_corte >= 0",
}
_CHECKS_VENTA = {
    "efectivo_lo_tiene_valido": (
        "efectivo_lo_tiene IS NULL OR efectivo_lo_tiene IN ('psd','organizador')"
    ),
    "terminal_de_valida": "terminal_de IS NULL OR terminal_de IN ('psd','organizador')",
}


def upgrade() -> None:
    op.add_column(
        "events",
        sa.Column("taquilla_comision_pct", sa.Numeric(precision=5, scale=2)),
    )
    op.add_column(
        "events",
        sa.Column(
            "taquilla_efectivo_lo_tiene",
            sa.String(length=12),
            server_default="organizador",
            nullable=False,
        ),
    )
    op.add_column(
        "events",
        sa.Column(
            "taquilla_terminal_de",
            sa.String(length=12),
            server_default="organizador",
            nullable=False,
        ),
    )
    op.add_column("events", sa.Column("taquilla_pie_recibo", sa.Text()))
    op.add_column(
        "events",
        sa.Column(
            "taquilla_formas_pago", postgresql.JSONB(astext_type=sa.Text())
        ),
    )
    op.add_column(
        "events",
        sa.Column(
            "taquilla_max_lugares", sa.Integer(), server_default="20", nullable=False
        ),
    )
    op.add_column(
        "events",
        sa.Column(
            "taquilla_reserva_cantidad",
            sa.Integer(),
            server_default="10",
            nullable=False,
        ),
    )
    op.add_column(
        "events", sa.Column("taquilla_reserva_zona_id", sa.BigInteger())
    )
    op.create_foreign_key(
        op.f("fk_events_taquilla_reserva_zona_id_zones"),
        "events",
        "zones",
        ["taquilla_reserva_zona_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.add_column(
        "events",
        sa.Column(
            "taquilla_tolerancia_corte",
            sa.Numeric(precision=10, scale=2),
            server_default="0",
            nullable=False,
        ),
    )
    for nombre, condicion in _CHECKS_EVENTO.items():
        op.create_check_constraint(op.f(f"ck_events_{nombre}"), "events", condicion)

    op.add_column(
        "box_office_sales", sa.Column("efectivo_lo_tiene", sa.String(length=12))
    )
    op.add_column("box_office_sales", sa.Column("terminal_de", sa.String(length=12)))
    for nombre, condicion in _CHECKS_VENTA.items():
        op.create_check_constraint(
            op.f(f"ck_box_office_sales_{nombre}"), "box_office_sales", condicion
        )


def downgrade() -> None:
    for nombre in _CHECKS_VENTA:
        op.drop_constraint(
            op.f(f"ck_box_office_sales_{nombre}"), "box_office_sales", type_="check"
        )
    op.drop_column("box_office_sales", "terminal_de")
    op.drop_column("box_office_sales", "efectivo_lo_tiene")
    for nombre in _CHECKS_EVENTO:
        op.drop_constraint(op.f(f"ck_events_{nombre}"), "events", type_="check")
    op.drop_constraint(
        op.f("fk_events_taquilla_reserva_zona_id_zones"), "events", type_="foreignkey"
    )
    for col in (
        "taquilla_tolerancia_corte",
        "taquilla_reserva_zona_id",
        "taquilla_reserva_cantidad",
        "taquilla_max_lugares",
        "taquilla_formas_pago",
        "taquilla_pie_recibo",
        "taquilla_terminal_de",
        "taquilla_efectivo_lo_tiene",
        "taquilla_comision_pct",
    ):
        op.drop_column("events", col)
