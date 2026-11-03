"""Sesiones y correo del equipo (una sola puerta para entrar).

`users.sesion_version` viaja en el token: subirla cierra sus sesiones (al
recuperar la contrasena o al ponerle una temporal). `users.email_verificado_en`
dice si probo que el correo es suyo, para ver tambien sus boletos. Solo
agrega: los tokens de antes llevan version 0, igual que todas las cuentas.

Revision ID: b7d2e9a4c6f1
Revises: a1e6c4d9f0b3
Create Date: 2026-11-02 20:00:00-06:00
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "b7d2e9a4c6f1"
down_revision: str | None = "a1e6c4d9f0b3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("sesion_version", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "users", sa.Column("email_verificado_en", sa.DateTime(timezone=True))
    )


def downgrade() -> None:
    op.drop_column("users", "email_verificado_en")
    op.drop_column("users", "sesion_version")
