"""add preferences_nudge_sent to users

Revision ID: e5f1a2b3c4d5
Revises: c9e4b2a7f1d3
Create Date: 2026-03-07

"""
from alembic import op
import sqlalchemy as sa

revision = "e5f1a2b3c4d5"
down_revision = "c9e4b2a7f1d3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "preferences_nudge_sent",
            sa.Boolean(),
            nullable=False,
            server_default="false",
        ),
    )


def downgrade() -> None:
    op.drop_column("users", "preferences_nudge_sent")
