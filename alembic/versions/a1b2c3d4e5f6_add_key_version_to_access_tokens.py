"""Add key_version to access_tokens

Revision ID: a1b2c3d4e5f6
Revises: df74fb3bc951
Create Date: 2026-03-15 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, Sequence[str], None] = 'df74fb3bc951'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add key_version column to access_tokens table."""
    op.add_column(
        'access_tokens',
        sa.Column('key_version', sa.Integer(), nullable=False, server_default='1'),
    )


def downgrade() -> None:
    """Remove key_version column from access_tokens table."""
    op.drop_column('access_tokens', 'key_version')
