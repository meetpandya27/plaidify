"""Add consent_requests and consent_grants tables

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-03-18 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd4e5f6a7b8c9'
down_revision: Union[str, Sequence[str], None] = 'c3d4e5f6a7b8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create consent_requests and consent_grants tables."""
    op.create_table(
        'consent_requests',
        sa.Column('id', sa.String(), primary_key=True),
        sa.Column('agent_name', sa.String(), nullable=False),
        sa.Column('agent_description', sa.Text(), nullable=True),
        sa.Column('scopes', sa.Text(), nullable=False),
        sa.Column('duration_seconds', sa.Integer(), nullable=False, server_default='3600'),
        sa.Column('access_token', sa.String(), sa.ForeignKey('access_tokens.token'), nullable=False),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('status', sa.String(), nullable=False, server_default='pending'),
        sa.Column('created_at', sa.DateTime(), nullable=True),
    )
    op.create_index('ix_consent_requests_id', 'consent_requests', ['id'])

    op.create_table(
        'consent_grants',
        sa.Column('token', sa.String(), primary_key=True),
        sa.Column('consent_request_id', sa.String(), sa.ForeignKey('consent_requests.id'), nullable=False),
        sa.Column('scopes', sa.Text(), nullable=False),
        sa.Column('access_token', sa.String(), sa.ForeignKey('access_tokens.token'), nullable=False),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('expires_at', sa.DateTime(), nullable=False),
        sa.Column('revoked', sa.Boolean(), server_default=sa.text('0')),
        sa.Column('created_at', sa.DateTime(), nullable=True),
    )
    op.create_index('ix_consent_grants_token', 'consent_grants', ['token'])


def downgrade() -> None:
    """Drop consent tables."""
    op.drop_index('ix_consent_grants_token', table_name='consent_grants')
    op.drop_table('consent_grants')
    op.drop_index('ix_consent_requests_id', table_name='consent_requests')
    op.drop_table('consent_requests')
