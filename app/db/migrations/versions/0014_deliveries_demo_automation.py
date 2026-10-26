"""deliveries log, demo phone, automation settings

Revision ID: 0014
Revises: 0013
Create Date: 2026-10-26 20:00:00
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0014'
down_revision: Union[str, None] = '0013'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('deliveries',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('decision_id', sa.Integer(), nullable=False),
    sa.Column('tenant_id', sa.Integer(), nullable=False),
    sa.Column('chat_id', sa.BigInteger(), nullable=False),
    sa.Column('kind', sa.String(length=10), nullable=False),
    sa.Column('status', sa.String(length=10), nullable=False),
    sa.Column('error', sa.String(length=300), nullable=True),
    sa.Column('telegram_message_id', sa.BigInteger(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['decision_id'], ['offer_decisions.id'], ),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_deliveries_decision_id'), 'deliveries', ['decision_id'], unique=False)
    op.create_index(op.f('ix_deliveries_tenant_id'), 'deliveries', ['tenant_id'], unique=False)
    op.add_column('tenants', sa.Column('demo_chat_id', sa.BigInteger(), nullable=True))
    op.add_column('tenants', sa.Column('automation', sa.JSON(), nullable=True))
    op.add_column('tenants', sa.Column('automation_state', sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column('tenants', 'automation_state')
    op.drop_column('tenants', 'automation')
    op.drop_column('tenants', 'demo_chat_id')
    op.drop_index(op.f('ix_deliveries_tenant_id'), table_name='deliveries')
    op.drop_index(op.f('ix_deliveries_decision_id'), table_name='deliveries')
    op.drop_table('deliveries')
