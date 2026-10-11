"""Round 5: personal data on items (tags, note, speaker names) and the user's own TOTP after
Google sign-in.

Revision ID: 0007
Revises: 0006
"""
import re

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

ME = "current_setting('dwyt.user_id')::bigint"

SCHEMA = """
ALTER TABLE items ADD COLUMN note text;

CREATE TABLE item_tags (
  item_id bigint NOT NULL REFERENCES items ON DELETE CASCADE,
  tag text NOT NULL CHECK (length(tag) BETWEEN 1 AND 40),
  user_id bigint NOT NULL REFERENCES users ON DELETE CASCADE,
  PRIMARY KEY (item_id, tag)
);
CREATE INDEX item_tags_user ON item_tags (user_id, tag);

-- the user's names for a transcript's speakers: {"Szereplő 1": "Kovács Anna"}
ALTER TABLE item_transcripts ADD COLUMN speaker_names jsonb NOT NULL DEFAULT '{}';

-- a code from the authenticator app after Google sign-in (the user's choice, or REQUIRE_TOTP)
ALTER TABLE users ADD COLUMN totp_after_google boolean NOT NULL DEFAULT false;
ALTER TABLE users ADD COLUMN totp_pending_secret text;
"""


def _role(name: str) -> str:
    if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", name):
        raise ValueError(f"bad role name: {name!r}")
    return name


def upgrade(app_role: str = "dwyt_app") -> None:
    role = _role(app_role)
    op.execute(SCHEMA)
    op.execute("ALTER TABLE item_tags ENABLE ROW LEVEL SECURITY")
    op.execute(f"CREATE POLICY own_rows ON item_tags USING (user_id = {ME}) WITH CHECK (user_id = {ME})")
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON item_tags TO {role}")


def downgrade(app_role: str = "dwyt_app") -> None:
    op.execute("""
        DROP TABLE item_tags;
        ALTER TABLE items DROP COLUMN note;
        ALTER TABLE item_transcripts DROP COLUMN speaker_names;
        ALTER TABLE users DROP COLUMN totp_after_google, DROP COLUMN totp_pending_secret;
    """)
