"""Own recordings (browser uploads staged on the central service, files in a runner's watched
folder) and per-user quotas.

Revision ID: 0003
Revises: 0002
"""
import re

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

ME = "current_setting('dwyt.user_id')::bigint"

SCHEMA = """
-- a file a user uploaded in the browser; staged under STAGING_DIR/<sha256> until a runner has it
CREATE TABLE uploads (
  id bigserial PRIMARY KEY,
  user_id bigint NOT NULL REFERENCES users ON DELETE CASCADE,
  sha256 text NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
  name text NOT NULL,
  size bigint NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  used_at timestamptz,       -- submitted as a job
  consumed_at timestamptz    -- the runner delivered the transcript (or the upload expired): file deletable
);
CREATE INDEX uploads_user ON uploads (user_id, consumed_at);
CREATE INDEX uploads_sha ON uploads (sha256);

-- admin overrides of the default quotas (NULL = the default)
ALTER TABLE users ADD COLUMN quota_storage_mb integer;
ALTER TABLE users ADD COLUMN quota_upload_mb integer;
"""


def _role(name: str) -> str:
    if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", name):
        raise ValueError(f"bad role name: {name!r}")
    return name


def upgrade(app_role: str = "dwyt_app") -> None:
    role = _role(app_role)
    op.execute(SCHEMA)
    op.execute("ALTER TABLE uploads ENABLE ROW LEVEL SECURITY")
    op.execute(f"CREATE POLICY own_rows ON uploads USING (user_id = {ME}) WITH CHECK (user_id = {ME})")
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON uploads TO {role}")
    op.execute(f"GRANT USAGE, SELECT ON SEQUENCE uploads_id_seq TO {role}")


def downgrade(app_role: str = "dwyt_app") -> None:
    op.execute("DROP TABLE uploads; ALTER TABLE users DROP COLUMN quota_storage_mb, DROP COLUMN quota_upload_mb;")
