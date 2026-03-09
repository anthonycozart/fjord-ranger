"""add input_tokens and output_tokens to agent_turns

Revision ID: f6a7b8c9d0e1
Revises: e5f1a2b3c4d5
Create Date: 2026-03-07

"""
from alembic import op
import sqlalchemy as sa

revision = "f6a7b8c9d0e1"
down_revision = "e5f1a2b3c4d5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("agent_turns", sa.Column("input_tokens", sa.Integer(), nullable=True))
    op.add_column("agent_turns", sa.Column("output_tokens", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("agent_turns", "output_tokens")
    op.drop_column("agent_turns", "input_tokens")
