"""Re-read every followed channel's feed soon after the update (round 8, decision 19.4): videos the inbox already had
before 0017 get their description at the next feed check, so make that check happen now instead of within the hour.

Revision ID: 0018
Revises: 0017
"""
from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade(app_role: str = "dwyt_app") -> None:
    op.execute("UPDATE channels SET last_checked_at = NULL WHERE id IN "
               "(SELECT channel_id FROM channel_videos WHERE description IS NULL)")


def downgrade(app_role: str = "dwyt_app") -> None:
    pass
