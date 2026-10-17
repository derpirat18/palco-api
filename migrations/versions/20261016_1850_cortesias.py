"""Cortesias: motivo, quien la otorgo y si se revoco (C1).

La cortesia es una orden con total $0 y canal 'cortesia'; esta tabla guarda
lo que la orden no dice. Tabla nueva: no toca datos.

Revision ID: bafe65d31d0f
Revises: 8c41f0d2a7b3
Create Date: 2026-10-16 18:50:56.556001-06:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = 'bafe65d31d0f'
down_revision: str | None = '8c41f0d2a7b3'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('cortesias',
    sa.Column('order_id', sa.BigInteger(), nullable=False),
    sa.Column('motivo', sa.String(length=120), nullable=False),
    sa.Column('otorgada_por', sa.BigInteger(), nullable=True),
    sa.Column('otorgada_en', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('revocada_por', sa.BigInteger(), nullable=True),
    sa.Column('revocada_en', sa.DateTime(timezone=True), nullable=True),
    sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
    sa.ForeignKeyConstraint(['order_id'], ['orders.id'], name=op.f('fk_cortesias_order_id_orders'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['otorgada_por'], ['users.id'], name=op.f('fk_cortesias_otorgada_por_users'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['revocada_por'], ['users.id'], name=op.f('fk_cortesias_revocada_por_users'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_cortesias')),
    sa.UniqueConstraint('order_id', name=op.f('uq_cortesias_order_id'))
    )


def downgrade() -> None:
    op.drop_table('cortesias')
