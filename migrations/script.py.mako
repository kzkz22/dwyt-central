"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
"""
from alembic import op

revision = ${repr(up_revision)}
down_revision = ${repr(down_revision)}
branch_labels = None
depends_on = None


def upgrade(app_role: str = "dwyt_app") -> None:
    ${upgrades if upgrades else "pass"}


def downgrade(app_role: str = "dwyt_app") -> None:
    ${downgrades if downgrades else "pass"}
