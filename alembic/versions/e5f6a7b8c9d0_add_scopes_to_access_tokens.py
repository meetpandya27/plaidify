"""Add scopes column to access_tokens

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-03-19 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e5f6a7b8c9d0'
down_revision: Union[str, Sequence[str], None] = 'd4e5f6a7b8c9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add scopes column to access_tokens table."""
    op.add_column('access_tokens', sa.Column('scopes', sa.Text(), nullable=True))


def downgrade() -> None:
    """Remove scopes column from access_tokens table."""
    op.drop_column('access_tokens', 'scopes')
