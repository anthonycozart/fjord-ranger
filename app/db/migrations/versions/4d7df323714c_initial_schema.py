"""initial schema

Revision ID: 4d7df323714c
Revises:
Create Date: 2026-02-28

Creates all five tables:
  users, user_criteria, slots, user_slot_states, notifications
"""

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision: str = "4d7df323714c"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("phone_number", sa.String(20), unique=True, nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("max_notifications_per_day", sa.SmallInteger(), nullable=False, server_default="3"),
        sa.Column("daily_notification_count", sa.SmallInteger(), nullable=False, server_default="0"),
        sa.Column("daily_count_reset_at", sa.Date(), nullable=False, server_default=sa.func.current_date()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    op.create_table(
        "user_criteria",
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), primary_key=True),
        sa.Column("criteria", JSONB(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    op.create_table(
        "slots",
        sa.Column("momence_id", sa.BigInteger(), primary_key=True),
        sa.Column("session_name", sa.String(100), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("duration_minutes", sa.SmallInteger(), nullable=False),
        sa.Column("location", sa.String(100), nullable=False),
        sa.Column("location_id", sa.Integer(), nullable=False),
        sa.Column("price_usd", sa.Numeric(8, 2), nullable=False),
        sa.Column("capacity", sa.SmallInteger(), nullable=False),
        sa.Column("remaining_spots", sa.SmallInteger(), nullable=False, server_default="0"),
        sa.Column("total_spots", sa.SmallInteger(), nullable=False),
        sa.Column("is_cancelled", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("booking_url", sa.Text(), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    # Index for the most common query: upcoming available slots, ordered by time
    op.create_index("ix_slots_starts_at", "slots", ["starts_at"])

    op.create_table(
        "user_slot_states",
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), primary_key=True),
        sa.Column("momence_id", sa.BigInteger(), sa.ForeignKey("slots.momence_id"), primary_key=True),
        sa.Column("state", sa.String(20), nullable=False, server_default="new"),
        sa.Column("notified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("feedback_raw", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    # Index for the analyzer's core query: find new/notified slots for a given user
    op.create_index("ix_user_slot_states_user_state", "user_slot_states", ["user_id", "state"])

    op.create_table(
        "notifications",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("slot_code", sa.String(6), unique=True, nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("momence_id", sa.BigInteger(), sa.ForeignKey("slots.momence_id"), nullable=False),
        sa.Column("twilio_message_sid", sa.String(50), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("notifications")
    op.drop_index("ix_user_slot_states_user_state", table_name="user_slot_states")
    op.drop_table("user_slot_states")
    op.drop_index("ix_slots_starts_at", table_name="slots")
    op.drop_table("slots")
    op.drop_table("user_criteria")
    op.drop_table("users")
