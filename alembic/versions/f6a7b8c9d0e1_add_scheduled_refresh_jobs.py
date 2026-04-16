"""Add scheduled_refresh_jobs table

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2025-01-01 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'f6a7b8c9d0e1'
down_revision = 'e5f6a7b8c9d0'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'scheduled_refresh_jobs',
        sa.Column('access_token', sa.String(), sa.ForeignKey('access_tokens.token'), primary_key=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('interval_seconds', sa.Integer(), nullable=False, server_default='3600'),
        sa.Column('enabled', sa.Boolean(), nullable=False, server_default='1'),
        sa.Column('last_refreshed', sa.DateTime(), nullable=True),
        sa.Column('last_error', sa.Text(), nullable=True),
        sa.Column('consecutive_failures', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('created_at', sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table('scheduled_refresh_jobs')
