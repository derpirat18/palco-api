"""Contacto por compra (OB1): el correo y el telefono escritos en cada orden.

El comprador es un registro por correo para toda la plataforma, y su
telefono es el de su primera compra, quiza con otro organizador. Ventas y
el Excel lo mostraban como si fuera de esta compra. Desde aqui cada orden
guarda el suyo.

Relleno de lo que ya existe:
- email_snap de todas: el correo del registro, que es exactamente el que se
  escribio (el registro se identifica por ese correo).
- telefono_snap solo de las compras web: el del registro, que en casi todos
  los casos es el de esa compra. Las cortesias se quedan sin telefono:
  quien las dio nunca escribio uno.

Solo agrega columnas nulas: el codigo anterior sigue funcionando mientras
se despliega el nuevo.

Revision ID: 9b4d2e7f1a63
Revises: 3c8e1f6a9d25
Create Date: 2026-10-23 13:00:00-06:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "9b4d2e7f1a63"
down_revision: str | None = "3c8e1f6a9d25"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("orders", sa.Column("email_snap", sa.String(255), nullable=True))
    op.add_column("orders", sa.Column("telefono_snap", sa.String(16), nullable=True))
    op.create_check_constraint(
        op.f("ck_orders_telefono_snap_e164"),
        "orders",
        r"telefono_snap IS NULL OR telefono_snap ~ '^\+[1-9][0-9]{7,14}$'",
    )
    op.execute(
        """
        UPDATE orders o
           SET email_snap = c.email,
               telefono_snap = CASE
                   WHEN o.canal IS DISTINCT FROM 'cortesia' THEN c.telefono
               END
          FROM customers c
         WHERE c.id = o.customer_id
        """
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_orders_telefono_snap_e164"), "orders", type_="check")
    op.drop_column("orders", "telefono_snap")
    op.drop_column("orders", "email_snap")
