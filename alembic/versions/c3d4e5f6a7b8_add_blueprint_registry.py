"""Add blueprint_registry table

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-03-19 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c3d4e5f6a7b8'
down_revision: Union[str, Sequence[str], None] = 'b2c3d4e5f6a7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create blueprint_registry table."""
    op.create_table(
        'blueprint_registry',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('name', sa.String(), nullable=False),
        sa.Column('site', sa.String(), nullable=False),
        sa.Column('domain', sa.String(), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('author', sa.String(), nullable=True),
        sa.Column('version', sa.String(), nullable=False, server_default='1.0.0'),
        sa.Column('schema_version', sa.String(), nullable=False, server_default='2'),
        sa.Column('tags', sa.Text(), nullable=True),
        sa.Column('has_mfa', sa.Boolean(), server_default=sa.text('0')),
        sa.Column('quality_tier', sa.String(), nullable=False, server_default='community'),
        sa.Column('blueprint_json', sa.Text(), nullable=False),
        sa.Column('extract_fields', sa.Text(), nullable=True),
        sa.Column('downloads', sa.Integer(), server_default='0'),
        sa.Column('published_by', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
    )
    op.create_index('ix_blueprint_registry_site', 'blueprint_registry', ['site'], unique=True)
    op.create_index('ix_blueprint_registry_id', 'blueprint_registry', ['id'])


def downgrade() -> None:
    """Drop blueprint_registry table."""
    op.drop_index('ix_blueprint_registry_id', table_name='blueprint_registry')
    op.drop_index('ix_blueprint_registry_site', table_name='blueprint_registry')
    op.drop_table('blueprint_registry')
