"""When a channel's feed started failing: YouTube's feed answers 404/500 for every channel for hours
at a time, so one failure says nothing about the channel; only a long run of failures does.

Revision ID: 0010
Revises: 0009
"""
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade(app_role: str = "dwyt_app") -> None:
    op.execute("ALTER TABLE channels ADD COLUMN failing_since timestamptz")
    # the errors stored so far came from that outage ("not found"): forget them, the next poll tells
    op.execute("UPDATE channels SET last_error = NULL")


def downgrade(app_role: str = "dwyt_app") -> None:
    op.execute("ALTER TABLE channels DROP COLUMN failing_since")
