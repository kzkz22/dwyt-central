"""Notifications (round 4b): each user's channel (ntfy or Home Assistant, secrets encrypted,
system role only) and an outbox of notifications (own rows).

Revision ID: 0005
Revises: 0004
"""
import re

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

ME = "current_setting('dwyt.user_id')::bigint"

SCHEMA = """
CREATE TABLE notify_settings (
  user_id bigint PRIMARY KEY REFERENCES users ON DELETE CASCADE,
  channel text NOT NULL DEFAULT 'none' CHECK (channel IN ('none', 'ntfy', 'ha')),
  ntfy_server text,
  ntfy_topic text,
  ntfy_token_enc text,
  ha_url text,
  ha_token_enc text,
  ha_service text,
  digest_time text NOT NULL DEFAULT '19:00' CHECK (digest_time ~ '^([01][0-9]|2[0-3]):[0-5][0-9]$'),
  on_manual boolean NOT NULL DEFAULT true,
  on_error boolean NOT NULL DEFAULT true,
  last_digest_on date,
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE notifications (
  id bigserial PRIMARY KEY,
  user_id bigint NOT NULL REFERENCES users ON DELETE CASCADE,
  kind text NOT NULL CHECK (kind IN ('done', 'error', 'digest', 'test')),
  digest boolean NOT NULL DEFAULT false,      -- waits for the daily digest
  item_id bigint REFERENCES items ON DELETE SET NULL,
  title text NOT NULL,
  message text NOT NULL,
  url text,
  created_at timestamptz NOT NULL DEFAULT now(),
  sent_at timestamptz,
  attempts integer NOT NULL DEFAULT 0,
  error text
);
CREATE INDEX notifications_pending ON notifications (user_id, digest, id) WHERE sent_at IS NULL;

-- an item's "ready" notification is made once
ALTER TABLE items ADD COLUMN notified_at timestamptz;
"""


def _role(name: str) -> str:
    if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", name):
        raise ValueError(f"bad role name: {name!r}")
    return name


def upgrade(app_role: str = "dwyt_app") -> None:
    role = _role(app_role)
    op.execute(SCHEMA)
    op.execute("ALTER TABLE notifications ENABLE ROW LEVEL SECURITY")
    op.execute(f"CREATE POLICY own_rows ON notifications USING (user_id = {ME}) WITH CHECK (user_id = {ME})")
    op.execute(f"GRANT SELECT ON notifications TO {role}")
    op.execute("ALTER TABLE notify_settings ENABLE ROW LEVEL SECURITY")  # no policy, no grant: system only


def downgrade(app_role: str = "dwyt_app") -> None:
    op.execute("DROP TABLE notifications, notify_settings; ALTER TABLE items DROP COLUMN notified_at;")
