"""MCP access tokens (round 4c): named, revocable, per user; only the hash is stored.

Revision ID: 0006
Revises: 0005
"""
import re

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

ME = "current_setting('dwyt.user_id')::bigint"

SCHEMA = """
CREATE TABLE mcp_tokens (
  id bigserial PRIMARY KEY,
  user_id bigint NOT NULL REFERENCES users ON DELETE CASCADE,
  name text NOT NULL,
  token_hash text NOT NULL UNIQUE,
  can_start boolean NOT NULL DEFAULT false,        -- may start transcriptions (atiras_inditasa)
  include_private boolean NOT NULL DEFAULT false,  -- sees own recordings (uploads, watched folders)
  created_at timestamptz NOT NULL DEFAULT now(),
  last_used_at timestamptz,
  revoked_at timestamptz
);
CREATE INDEX mcp_tokens_user ON mcp_tokens (user_id);
"""


def _role(name: str) -> str:
    if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", name):
        raise ValueError(f"bad role name: {name!r}")
    return name


def upgrade(app_role: str = "dwyt_app") -> None:
    role = _role(app_role)
    op.execute(SCHEMA)
    op.execute("ALTER TABLE mcp_tokens ENABLE ROW LEVEL SECURITY")
    op.execute(f"CREATE POLICY own_rows ON mcp_tokens USING (user_id = {ME}) WITH CHECK (user_id = {ME})")
    op.execute(f"GRANT SELECT, INSERT, UPDATE ON mcp_tokens TO {role}")
    op.execute(f"GRANT USAGE, SELECT ON SEQUENCE mcp_tokens_id_seq TO {role}")


def downgrade(app_role: str = "dwyt_app") -> None:
    op.execute("DROP TABLE mcp_tokens")
