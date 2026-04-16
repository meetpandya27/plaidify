"""add agents table and audit_logs columns (agent_id, ip_address)

Revision ID: h8i9j0k1l2m3
Revises: g7h8i9j0k1l2
Create Date: 2026-04-22 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "h8i9j0k1l2m3"
down_revision = "g7h8i9j0k1l2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- agents table ---
    op.create_table(
        "agents",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("description", sa.String(), nullable=True),
        sa.Column("owner_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("api_key_id", sa.String(), sa.ForeignKey("api_keys.id"), nullable=True),
        sa.Column("allowed_scopes", sa.Text(), nullable=True),
        sa.Column("allowed_sites", sa.Text(), nullable=True),
        sa.Column("rate_limit", sa.Integer(), default=60),
        sa.Column("is_active", sa.Boolean(), default=True, nullable=False),
        sa.Column("last_active_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_agents_id", "agents", ["id"])
    op.create_index("ix_agents_owner_id", "agents", ["owner_id"])

    # --- audit_logs new columns ---
    op.add_column("audit_logs", sa.Column("agent_id", sa.String(), nullable=True))
    op.add_column("audit_logs", sa.Column("ip_address", sa.String(45), nullable=True))
    op.create_index("ix_audit_logs_agent_id", "audit_logs", ["agent_id"])


def downgrade() -> None:
    # --- audit_logs columns ---
    op.drop_index("ix_audit_logs_agent_id", table_name="audit_logs")
    op.drop_column("audit_logs", "ip_address")
    op.drop_column("audit_logs", "agent_id")

    # --- agents table ---
    op.drop_index("ix_agents_owner_id", table_name="agents")
    op.drop_index("ix_agents_id", table_name="agents")
    op.drop_table("agents")
