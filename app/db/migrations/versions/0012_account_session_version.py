"""account session version (logout / password change end older sessions)

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-26 12:00:00
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0012'
down_revision: Union[str, None] = '0011'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('accounts', sa.Column('session_version', sa.Integer(), server_default='0', nullable=False))


def downgrade() -> None:
    op.drop_column('accounts', 'session_version')
