"""Sin cargos al comprador: el comprador paga solo el precio del boleto.

PSD cobra su comision al organizador, no al comprador (ADR 0012). Se
quitan los cargos de la configuracion (clients, events) y de las ordenes
(orders, order_items), y la base exige desde ahora que lo cobrado sea el
precio: total = subtotal - descuento, importe = precio_unitario - descuento.

Sobre los datos que se borran:
- No hubo ventas reales: solo ordenes de prueba en desarrollo.
- Una orden de prueba con cargos conserva su total; la diferencia contra
  el subtotal sigue mostrando lo que se cobro de cargos.
- Esas filas viejas no cumplen la regla nueva. Si existen, las
  restricciones se crean NOT VALID: se exigen a todo lo nuevo y no se
  reescribe el pasado. Si no existen, se crean validadas.
- El downgrade regresa las columnas en cero: la configuracion anterior de
  cargos no se puede reconstruir.

Revision ID: 4664b4db97b4
Revises: 53d6874ad968
Create Date: 2026-10-14 22:52:00-06:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "4664b4db97b4"
down_revision: str | None = "53d6874ad968"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _agregar_check(tabla: str, nombre: str, condicion: str) -> None:
    conn = op.get_bind()
    incumplen = conn.scalar(
        sa.text(f"SELECT count(*) FROM {tabla} WHERE NOT ({condicion})")
    )
    sufijo = " NOT VALID" if incumplen else ""
    op.execute(
        f"ALTER TABLE {tabla} ADD CONSTRAINT {nombre} CHECK ({condicion}){sufijo}"
    )


def upgrade() -> None:
    op.drop_constraint(
        op.f("ck_clients_cargo_servicio_rango"), "clients", type_="check"
    )
    op.drop_column("clients", "cargo_orden")
    op.drop_column("clients", "cargo_servicio_pct")
    op.drop_column("events", "cargo_orden")
    op.drop_column("events", "cargo_servicio_pct")
    op.drop_column("order_items", "cargo_servicio")
    op.drop_column("orders", "cargo_orden")
    op.drop_column("orders", "cargo_servicio")
    op.drop_column("orders", "cargo_servicio_pct_snap")
    op.drop_column("orders", "cargo_orden_snap")

    _agregar_check(
        "orders", "ck_orders_total_es_el_precio", "total = subtotal - descuento"
    )
    _agregar_check(
        "order_items",
        "ck_order_items_importe_es_el_precio",
        "importe = precio_unitario - descuento",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_order_items_importe_es_el_precio"), "order_items", type_="check"
    )
    op.drop_constraint(op.f("ck_orders_total_es_el_precio"), "orders", type_="check")

    cero = sa.text("0")
    op.add_column(
        "orders",
        sa.Column(
            "cargo_orden_snap", sa.Numeric(10, 2), nullable=False, server_default=cero
        ),
    )
    op.add_column(
        "orders",
        sa.Column(
            "cargo_servicio_pct_snap",
            sa.Numeric(5, 2),
            nullable=False,
            server_default=cero,
        ),
    )
    op.add_column(
        "orders",
        sa.Column(
            "cargo_servicio", sa.Numeric(12, 2), nullable=False, server_default=cero
        ),
    )
    op.add_column(
        "orders",
        sa.Column(
            "cargo_orden", sa.Numeric(10, 2), nullable=False, server_default=cero
        ),
    )
    op.add_column(
        "order_items",
        sa.Column(
            "cargo_servicio", sa.Numeric(10, 2), nullable=False, server_default=cero
        ),
    )
    op.add_column(
        "events", sa.Column("cargo_servicio_pct", sa.Numeric(5, 2), nullable=True)
    )
    op.add_column("events", sa.Column("cargo_orden", sa.Numeric(10, 2), nullable=True))
    op.add_column(
        "clients",
        sa.Column(
            "cargo_servicio_pct", sa.Numeric(5, 2), nullable=False, server_default=cero
        ),
    )
    op.add_column(
        "clients",
        sa.Column(
            "cargo_orden", sa.Numeric(10, 2), nullable=False, server_default=cero
        ),
    )
    op.create_check_constraint(
        op.f("ck_clients_cargo_servicio_rango"),
        "clients",
        "cargo_servicio_pct >= 0 AND cargo_servicio_pct <= 100",
    )
    for tabla, columna in (
        ("orders", "cargo_orden_snap"),
        ("orders", "cargo_servicio_pct_snap"),
        ("orders", "cargo_servicio"),
        ("orders", "cargo_orden"),
        ("order_items", "cargo_servicio"),
        ("clients", "cargo_servicio_pct"),
        ("clients", "cargo_orden"),
    ):
        op.alter_column(tabla, columna, server_default=None)
