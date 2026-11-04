"""Asistente de PSD por WhatsApp: conversaciones y mensajes (bot, 3 nov).

Dos tablas nuevas; no toca ninguna existente.

Revision ID: f2a7c9e4b1d8
Revises: e5b9c3d7a2f4
Create Date: 2026-11-03 23:00:00-06:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "f2a7c9e4b1d8"
down_revision: str | None = "e5b9c3d7a2f4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "asistente_conversaciones",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column(
            "uuid",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("canal", sa.String(length=20), nullable=False),
        sa.Column("telefono", sa.String(length=20), nullable=False),
        sa.Column("nombre_perfil", sa.String(length=120), nullable=True),
        sa.Column("estado", sa.String(length=20), nullable=False),
        sa.Column(
            "ultimo_mensaje_en",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "creada_en",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "canal IN ('whatsapp')", name=op.f("ck_asistente_conversaciones_canal")
        ),
        sa.CheckConstraint(
            "estado IN ('bot', 'persona')",
            name=op.f("ck_asistente_conversaciones_estado"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_asistente_conversaciones")),
        sa.UniqueConstraint(
            "canal", "telefono", name=op.f("uq_asistente_conversaciones_canal")
        ),
        sa.UniqueConstraint("uuid", name=op.f("uq_asistente_conversaciones_uuid")),
    )
    op.create_table(
        "asistente_mensajes",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("conversacion_id", sa.BigInteger(), nullable=False),
        sa.Column("direccion", sa.String(length=10), nullable=False),
        sa.Column("wamid", sa.String(length=160), nullable=True),
        sa.Column("tipo", sa.String(length=20), nullable=False),
        sa.Column("texto", sa.Text(), nullable=False),
        sa.Column("estado", sa.String(length=20), nullable=False),
        sa.Column("intentos", sa.Integer(), nullable=False),
        sa.Column(
            "creado_en",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "direccion IN ('entrada', 'salida')",
            name=op.f("ck_asistente_mensajes_direccion"),
        ),
        sa.CheckConstraint(
            "estado IN ('pendiente', 'respondido', 'ignorado', 'error', 'enviado')",
            name=op.f("ck_asistente_mensajes_estado"),
        ),
        sa.ForeignKeyConstraint(
            ["conversacion_id"],
            ["asistente_conversaciones.id"],
            name=op.f("fk_asistente_mensajes_conversacion_id_asistente_conversaciones"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_asistente_mensajes")),
        sa.UniqueConstraint("wamid", name=op.f("uq_asistente_mensajes_wamid")),
    )
    op.create_index(
        "ix_asistente_mensajes_conversacion_creado",
        "asistente_mensajes",
        ["conversacion_id", "creado_en"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_asistente_mensajes_conversacion_creado", table_name="asistente_mensajes"
    )
    op.drop_table("asistente_mensajes")
    op.drop_table("asistente_conversaciones")
