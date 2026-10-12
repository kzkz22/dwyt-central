"""Runner sharing (decision 17.5): an owner offers a runner to another dwyt user, with what it may
do for them and monthly cloud limits; the usage is counted per calendar month.

Revision ID: 0012
Revises: 0011
"""
import re

from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None

ME = "current_setting('dwyt.user_id')::bigint"

SCHEMA = """
CREATE TABLE runner_shares (
  id bigserial PRIMARY KEY,
  runner_id bigint NOT NULL REFERENCES runners ON DELETE CASCADE,
  owner_id bigint NOT NULL REFERENCES users ON DELETE CASCADE,
  user_id bigint NOT NULL REFERENCES users ON DELETE CASCADE,   -- who may use it
  allow_captions boolean NOT NULL DEFAULT true,
  allow_local boolean NOT NULL DEFAULT true,
  allow_llm boolean NOT NULL DEFAULT true,
  allow_own boolean NOT NULL DEFAULT false,                     -- the user's own recordings (uploads)
  cloud_hours double precision NOT NULL DEFAULT 0 CHECK (cloud_hours >= 0 AND cloud_hours <= 1000),
  llm_answers integer NOT NULL DEFAULT 0 CHECK (llm_answers >= 0 AND llm_answers <= 100000),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  accepted_at timestamptz,                                      -- NULL: offered, not accepted yet
  UNIQUE (runner_id, user_id),
  CHECK (owner_id <> user_id)
);
CREATE INDEX runner_shares_user ON runner_shares (user_id);

CREATE TABLE share_usage (
  share_id bigint NOT NULL REFERENCES runner_shares ON DELETE CASCADE,
  month text NOT NULL,                                          -- YYYY-MM in the instance's time zone
  cloud_seconds double precision NOT NULL DEFAULT 0,
  llm_answers integer NOT NULL DEFAULT 0,
  llm_tokens bigint NOT NULL DEFAULT 0,
  PRIMARY KEY (share_id, month)
);
"""


def _role(name: str) -> str:
    if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", name):
        raise ValueError(f"bad role name: {name!r}")
    return name


def upgrade(app_role: str = "dwyt_app") -> None:
    role = _role(app_role)
    op.execute(SCHEMA)
    # changes go through the system role with explicit checks (sharing.py); users only read
    op.execute("ALTER TABLE runner_shares ENABLE ROW LEVEL SECURITY")
    op.execute(f"CREATE POLICY own_shares ON runner_shares FOR SELECT USING (owner_id = {ME} OR user_id = {ME})")
    op.execute("ALTER TABLE share_usage ENABLE ROW LEVEL SECURITY")
    op.execute("CREATE POLICY own_share_usage ON share_usage FOR SELECT USING (EXISTS (SELECT 1 FROM runner_shares rs "
               f"WHERE rs.id = share_usage.share_id AND (rs.owner_id = {ME} OR rs.user_id = {ME})))")
    op.execute(f"GRANT SELECT ON runner_shares, share_usage TO {role}")


def downgrade(app_role: str = "dwyt_app") -> None:
    op.execute("DROP TABLE share_usage; DROP TABLE runner_shares;")
