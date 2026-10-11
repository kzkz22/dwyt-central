"""YouTube subscriptions (round 4a): channels and their feed videos (shared, polled once for
everyone), each user's subscriptions and inbox, and the user's link to the YouTube Data API
(an encrypted refresh token, readable by the system role only).

Revision ID: 0004
Revises: 0003
"""
import re

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

ME = "current_setting('dwyt.user_id')::bigint"

SCHEMA = """
CREATE TABLE channels (
  id text PRIMARY KEY CHECK (id ~ '^UC[A-Za-z0-9_-]{22}$'),
  title text,
  last_checked_at timestamptz,
  last_error text,
  created_at timestamptz NOT NULL DEFAULT now()
);

-- the videos seen in the channels' feeds; the Shorts check is done once per video
CREATE TABLE channel_videos (
  video_id text PRIMARY KEY CHECK (video_id ~ '^[A-Za-z0-9_-]{11}$'),
  channel_id text NOT NULL REFERENCES channels ON DELETE CASCADE,
  title text,
  published_at timestamptz,
  is_short boolean,
  seen_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX channel_videos_channel ON channel_videos (channel_id, published_at DESC);

CREATE TABLE subscriptions (
  id bigserial PRIMARY KEY,
  user_id bigint NOT NULL REFERENCES users ON DELETE CASCADE,
  channel_id text NOT NULL REFERENCES channels,
  source text NOT NULL CHECK (source IN ('google', 'takeout', 'manual')),
  active boolean NOT NULL DEFAULT true,
  rule text NOT NULL DEFAULT 'ask' CHECK (rule IN ('always', 'ask', 'never')),
  allow_cloud boolean NOT NULL DEFAULT false,
  notify text NOT NULL DEFAULT 'digest' CHECK (notify IN ('digest', 'instant', 'none')),
  baseline_at timestamptz NOT NULL DEFAULT now(),   -- only videos published after this are new
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (user_id, channel_id)
);
CREATE INDEX subscriptions_channel ON subscriptions (channel_id) WHERE active;

CREATE TABLE inbox_entries (
  id bigserial PRIMARY KEY,
  user_id bigint NOT NULL REFERENCES users ON DELETE CASCADE,
  channel_id text NOT NULL REFERENCES channels,
  video_id text NOT NULL,
  title text,
  published_at timestamptz,
  state text NOT NULL DEFAULT 'new' CHECK (state IN ('new', 'queued', 'done', 'skipped', 'failed', 'short')),
  item_id bigint REFERENCES items ON DELETE SET NULL,
  job_id bigint REFERENCES jobs ON DELETE SET NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (user_id, video_id)
);
CREATE INDEX inbox_entries_state ON inbox_entries (user_id, state, published_at DESC);

-- the user's consent to read the subscription list (system role only: the app role never sees tokens)
CREATE TABLE youtube_links (
  user_id bigint PRIMARY KEY REFERENCES users ON DELETE CASCADE,
  email text,
  refresh_token_enc text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  synced_at timestamptz,
  sync_error text,
  sync_count integer
);

-- a Google sign-in started for another purpose than signing in (connecting YouTube)
ALTER TABLE login_states ADD COLUMN purpose text NOT NULL DEFAULT 'login';
ALTER TABLE login_states ADD COLUMN user_id bigint REFERENCES users ON DELETE CASCADE;
"""

SUBSCRIBED = f"EXISTS (SELECT 1 FROM subscriptions s WHERE s.user_id = {ME} AND s.channel_id = "


def _role(name: str) -> str:
    if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", name):
        raise ValueError(f"bad role name: {name!r}")
    return name


def upgrade(app_role: str = "dwyt_app") -> None:
    role = _role(app_role)
    op.execute(SCHEMA)
    for table in ("subscriptions", "inbox_entries"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"CREATE POLICY own_rows ON {table} USING (user_id = {ME}) WITH CHECK (user_id = {ME})")
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {role}")
        op.execute(f"GRANT USAGE, SELECT ON SEQUENCE {table}_id_seq TO {role}")
    op.execute("ALTER TABLE channels ENABLE ROW LEVEL SECURITY")
    op.execute(f"CREATE POLICY through_subscriptions ON channels FOR SELECT USING ({SUBSCRIBED}channels.id))")
    op.execute("ALTER TABLE channel_videos ENABLE ROW LEVEL SECURITY")
    op.execute(f"CREATE POLICY through_subscriptions ON channel_videos FOR SELECT "
               f"USING ({SUBSCRIBED}channel_videos.channel_id))")
    op.execute(f"GRANT SELECT ON channels, channel_videos TO {role}")
    op.execute("ALTER TABLE youtube_links ENABLE ROW LEVEL SECURITY")  # no policy, no grant: system only


def downgrade(app_role: str = "dwyt_app") -> None:
    op.execute("""
        DROP TABLE youtube_links, inbox_entries, subscriptions, channel_videos, channels;
        ALTER TABLE login_states DROP COLUMN purpose, DROP COLUMN user_id;
    """)
