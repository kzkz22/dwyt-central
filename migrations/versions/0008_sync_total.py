"""The number of subscriptions YouTube reports, next to the number read (to spot gaps).

Revision ID: 0008
Revises: 0007
"""
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade(app_role: str = "dwyt_app") -> None:
    op.execute("ALTER TABLE youtube_links ADD COLUMN sync_total integer")


def downgrade(app_role: str = "dwyt_app") -> None:
    op.execute("ALTER TABLE youtube_links DROP COLUMN sync_total")
