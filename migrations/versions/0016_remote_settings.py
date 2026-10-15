"""Remote settings and the one-line install (round 7d, decisions 18.11, 18.16, 18.17).

runners: what the runner last reported about its settings (description, plain values, which keys are set,
its public key for end-to-end encrypted keys), whether it allows remote settings, how it is installed and
the newest version it knows of. runner_setting_changes: the owner's changes waiting for / taken by the
runner, plus changes made on the runner's machine; the log of both (encrypted keys are dropped once the
runner has answered). runner_install_codes: 5-minute, single-use codes of the install command.

Revision ID: 0016
Revises: 0015
"""
import re

from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None

ME = "current_setting('dwyt.user_id')::bigint"

SCHEMA = """
ALTER TABLE runners ADD COLUMN settings jsonb NOT NULL DEFAULT '{}';
ALTER TABLE runners ADD COLUMN settings_at timestamptz;
ALTER TABLE runners ADD COLUMN remote_settings boolean NOT NULL DEFAULT false;
ALTER TABLE runners ADD COLUMN install text;
ALTER TABLE runners ADD COLUMN latest_version text;
ALTER TABLE runners ADD COLUMN public_key text;

CREATE TABLE runner_setting_changes (
  id bigserial PRIMARY KEY,
  runner_id bigint NOT NULL REFERENCES runners ON DELETE CASCADE,
  user_id bigint NOT NULL REFERENCES users ON DELETE CASCADE,
  origin text NOT NULL CHECK (origin IN ('dwyt', 'runner')),
  by_name text,
  created_at timestamptz NOT NULL DEFAULT now(),
  lines jsonb NOT NULL DEFAULT '[]',          -- [{"key", "label", "old", "new"}] as shown in the log
  payload_values jsonb NOT NULL DEFAULT '{}', -- key -> text, delivered to the runner
  secrets jsonb,                              -- key -> base64 ciphertext; NULL once answered
  clear_keys jsonb NOT NULL DEFAULT '[]',
  action text CHECK (action IN ('update')),
  state text NOT NULL CHECK (state IN ('pending', 'delivered', 'done', 'partial', 'refused', 'failed', 'local')),
  delivered_at timestamptz,
  finished_at timestamptz,
  result jsonb NOT NULL DEFAULT '{}'
);
CREATE INDEX runner_setting_changes_runner ON runner_setting_changes (runner_id, id);

CREATE TABLE runner_install_codes (
  id bigserial PRIMARY KEY,
  user_id bigint NOT NULL REFERENCES users ON DELETE CASCADE,
  code_hash text NOT NULL UNIQUE,
  created_at timestamptz NOT NULL DEFAULT now(),
  expires_at timestamptz NOT NULL,
  used_at timestamptz,
  runner_id bigint REFERENCES runners ON DELETE SET NULL
);
"""


def _role(name: str) -> str:
    if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", name):
        raise ValueError(f"bad role name: {name!r}")
    return name


def upgrade(app_role: str = "dwyt_app") -> None:
    role = _role(app_role)
    op.execute(SCHEMA)
    op.execute("ALTER TABLE notifications DROP CONSTRAINT IF EXISTS notifications_kind_check")
    op.execute("ALTER TABLE notifications ADD CONSTRAINT notifications_kind_check "
               "CHECK (kind IN ('done', 'error', 'digest', 'test', 'runner'))")
    # changes are written through the system role with explicit owner checks (remote.py); users only read
    op.execute("ALTER TABLE runner_setting_changes ENABLE ROW LEVEL SECURITY")
    op.execute(f"CREATE POLICY own_changes ON runner_setting_changes FOR SELECT USING (user_id = {ME})")
    op.execute(f"GRANT SELECT ON runner_setting_changes TO {role}")
    op.execute("ALTER TABLE runner_install_codes ENABLE ROW LEVEL SECURITY")  # system role only


def downgrade(app_role: str = "dwyt_app") -> None:
    op.execute("""
        DROP TABLE runner_install_codes;
        DROP TABLE runner_setting_changes;
        ALTER TABLE notifications DROP CONSTRAINT IF EXISTS notifications_kind_check;
        ALTER TABLE notifications ADD CONSTRAINT notifications_kind_check
          CHECK (kind IN ('done', 'error', 'digest', 'test'));
        ALTER TABLE runners DROP COLUMN settings, DROP COLUMN settings_at, DROP COLUMN remote_settings,
          DROP COLUMN install, DROP COLUMN latest_version, DROP COLUMN public_key;
    """)
