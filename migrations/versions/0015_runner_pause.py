"""Switching a runner off without removing it (decision 18.14): an own runner per its owner
(runners.paused_at), a shared one per the user it is shared with (runner_shares.paused_at). A switched-off
runner gets no new jobs for that user; jobs already running finish.

Revision ID: 0015
Revises: 0014
"""
from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade(app_role: str = "dwyt_app") -> None:
    op.execute("""
        ALTER TABLE runners ADD COLUMN paused_at timestamptz;
        ALTER TABLE runner_shares ADD COLUMN paused_at timestamptz;
    """)


def downgrade(app_role: str = "dwyt_app") -> None:
    op.execute("""
        ALTER TABLE runner_shares DROP COLUMN paused_at;
        ALTER TABLE runners DROP COLUMN paused_at;
    """)
