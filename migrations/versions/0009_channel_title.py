"""The title of the YouTube channel a user's consent belongs to (shown on the Channels page).

Revision ID: 0009
Revises: 0008
"""
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade(app_role: str = "dwyt_app") -> None:
    op.execute("ALTER TABLE youtube_links ADD COLUMN channel_title text")


def downgrade(app_role: str = "dwyt_app") -> None:
    op.execute("ALTER TABLE youtube_links DROP COLUMN channel_title")
