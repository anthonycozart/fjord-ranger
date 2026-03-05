"""add messages and agent_turns tables

Revision ID: c9e4b2a7f1d3
Revises: 4d7df323714c
Create Date: 2026-03-04

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "c9e4b2a7f1d3"
down_revision = "4d7df323714c"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "messages",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(10), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_messages_user_created",
        "messages",
        ["user_id", sa.text("created_at DESC")],
    )

    op.create_table(
        "agent_turns",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("message_in", sa.Text(), nullable=False),
        sa.Column("thinking_text", sa.Text(), nullable=True),
        sa.Column("tools_called", postgresql.JSONB(), nullable=True),
        sa.Column("response_out", sa.Text(), nullable=False),
        sa.Column("model", sa.String(50), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_agent_turns_user_created",
        "agent_turns",
        ["user_id", sa.text("created_at DESC")],
    )


def downgrade() -> None:
    op.drop_index("ix_agent_turns_user_created", table_name="agent_turns")
    op.drop_table("agent_turns")
    op.drop_index("ix_messages_user_created", table_name="messages")
    op.drop_table("messages")
