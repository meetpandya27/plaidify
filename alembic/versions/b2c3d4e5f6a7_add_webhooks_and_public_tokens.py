"""Add webhooks and public_tokens tables

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-03-18 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b2c3d4e5f6a7'
down_revision: Union[str, Sequence[str], None] = 'a1b2c3d4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create webhooks and public_tokens tables."""
    op.create_table(
        'webhooks',
        sa.Column('id', sa.String(), primary_key=True),
        sa.Column('link_token', sa.String(), nullable=False, index=True),
        sa.Column('url', sa.Text(), nullable=False),
        sa.Column('secret', sa.Text(), nullable=False),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=True),
    )
    op.create_table(
        'public_tokens',
        sa.Column('token', sa.String(), primary_key=True),
        sa.Column('link_token', sa.String(), nullable=False),
        sa.Column('access_token', sa.String(), sa.ForeignKey('access_tokens.token'), nullable=False),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('exchanged', sa.Boolean(), default=False),
        sa.Column('expires_at', sa.DateTime(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    """Drop webhooks and public_tokens tables."""
    op.drop_table('public_tokens')
    op.drop_table('webhooks')
