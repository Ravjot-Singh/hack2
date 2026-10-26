"""guardrails (pause switch, blocked decisions) and customer preferences

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-26 15:00:00
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0013'
down_revision: Union[str, None] = '0012'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('tenants', sa.Column('paused', sa.Boolean(), server_default='false', nullable=False))
    op.add_column('offer_decisions', sa.Column('guardrail', sa.JSON(), nullable=True))
    op.create_table('customer_preferences',
    sa.Column('customer_id', sa.Integer(), nullable=False),
    sa.Column('tenant_id', sa.Integer(), nullable=False),
    sa.Column('categories', sa.JSON(), nullable=False),
    sa.Column('offer_types', sa.JSON(), nullable=False),
    sa.Column('quiet', sa.String(length=20), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['customer_id'], ['customers.id'], ),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ),
    sa.PrimaryKeyConstraint('customer_id')
    )
    op.create_index(op.f('ix_customer_preferences_tenant_id'), 'customer_preferences', ['tenant_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_customer_preferences_tenant_id'), table_name='customer_preferences')
    op.drop_table('customer_preferences')
    op.drop_column('offer_decisions', 'guardrail')
    op.drop_column('tenants', 'paused')
