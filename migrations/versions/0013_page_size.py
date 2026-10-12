"""The user's page size for the lists (15 / 25 / 50), remembered across devices.

Revision ID: 0013
Revises: 0012
"""
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade(app_role: str = "dwyt_app") -> None:
    op.execute("ALTER TABLE users ADD COLUMN page_size integer NOT NULL DEFAULT 25 CHECK (page_size IN (15, 25, 50))")


def downgrade(app_role: str = "dwyt_app") -> None:
    op.execute("ALTER TABLE users DROP COLUMN page_size")
