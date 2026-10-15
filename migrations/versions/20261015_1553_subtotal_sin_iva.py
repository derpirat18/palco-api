"""Subtotal sin IVA: subtotal + IVA - descuento = total, como en una factura.

Antes el subtotal llevaba el IVA adentro y el IVA era solo informativo,
lo que no cuadra con como se lee una factura. Ahora el subtotal es la
base sin IVA (ADR 0013). Lo que paga el comprador (total) no cambia.

Datos existentes:
- Ordenes hechas con la regla anterior (total = subtotal - descuento y
  con IVA desglosado): se les resta el IVA del subtotal. Su total queda
  igual.
- Ordenes de prueba de antes de los ADR 0012 y 0013 (con cargos al
  comprador) no cuadran con ninguna de las dos reglas. Si existen, la
  restriccion nueva se crea NOT VALID: se exige a todo lo nuevo y no se
  reescribe el pasado.

Revision ID: 0720994ec244
Revises: 3b75bd93a49c
Create Date: 2026-10-15 15:53:00-06:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0720994ec244"
down_revision: str | None = "3b75bd93a49c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _agregar_check(tabla: str, nombre: str, condicion: str) -> None:
    conn = op.get_bind()
    incumplen = conn.scalar(sa.text(f"SELECT count(*) FROM {tabla} WHERE NOT ({condicion})"))
    sufijo = " NOT VALID" if incumplen else ""
    op.execute(f"ALTER TABLE {tabla} ADD CONSTRAINT {nombre} CHECK ({condicion}){sufijo}")


def upgrade() -> None:
    # Primero se quita la regla vieja: aun NOT VALID, se exige en cada UPDATE.
    op.drop_constraint(op.f("ck_orders_total_es_el_precio"), "orders", type_="check")
    op.execute(
        "UPDATE orders SET subtotal = subtotal - iva "
        "WHERE iva > 0 AND total = subtotal - descuento"
    )
    _agregar_check("orders", "ck_orders_total_cuadra", "total = subtotal + iva - descuento")


def downgrade() -> None:
    op.drop_constraint(op.f("ck_orders_total_cuadra"), "orders", type_="check")
    op.execute(
        "UPDATE orders SET subtotal = subtotal + iva "
        "WHERE iva > 0 AND total = subtotal + iva - descuento"
    )
    _agregar_check("orders", "ck_orders_total_es_el_precio", "total = subtotal - descuento")
