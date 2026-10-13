"""Caption use (decision 18.9): the user's rule for YouTube captions, with a per-channel override.

Revision ID: 0014
Revises: 0013
"""
from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade(app_role: str = "dwyt_app") -> None:
    op.execute("""
        ALTER TABLE users ADD COLUMN captions_policy text NOT NULL DEFAULT 'manual'
          CHECK (captions_policy IN ('manual', 'any', 'never'));
        ALTER TABLE users ADD COLUMN captions_auto_langs text NOT NULL DEFAULT '';
        ALTER TABLE subscriptions ADD COLUMN captions_policy text
          CHECK (captions_policy IN ('manual', 'any', 'never'));
    """)


def downgrade(app_role: str = "dwyt_app") -> None:
    op.execute("""
        ALTER TABLE subscriptions DROP COLUMN captions_policy;
        ALTER TABLE users DROP COLUMN captions_auto_langs;
        ALTER TABLE users DROP COLUMN captions_policy;
    """)
