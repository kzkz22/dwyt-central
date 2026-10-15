"""Round 8 (plan 19a, decisions 19.1-19.5): the Megnézendő list in the user's YouTube account, read marks,
video descriptions in the inbox, and how sharing from the phone works.

- watch_later: the videos on the user's "dwyt – megnézendő" YouTube playlist (playlist item id kept, so a
  removal on dwyt removes it there, and the daily sync notices removals made on YouTube);
- youtube_links.can_write / watch_playlist_id: the write consent and the playlist dwyt keeps;
- items.read_at: when the item was read (opening its page marks it); existing items count as read;
- channel_videos.description: the start of the video's description from the channel feed;
- users.share_mode: ask (a page with the choices) or start (at once, with the usual settings).

Revision ID: 0017
Revises: 0016
"""
import re

from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None

ME = "current_setting('dwyt.user_id')::bigint"

SCHEMA = """
ALTER TABLE items ADD COLUMN read_at timestamptz;
UPDATE items SET read_at = now();
ALTER TABLE channel_videos ADD COLUMN description text;
ALTER TABLE youtube_links ADD COLUMN can_write boolean NOT NULL DEFAULT false;
ALTER TABLE youtube_links ADD COLUMN watch_playlist_id text;
-- false: connected only for the Megnézendő list (the subscriptions are not taken over then)
ALTER TABLE youtube_links ADD COLUMN subs_sync boolean NOT NULL DEFAULT true;
ALTER TABLE users ADD COLUMN share_mode text NOT NULL DEFAULT 'ask' CHECK (share_mode IN ('ask', 'start'));

CREATE TABLE watch_later (
  id bigserial PRIMARY KEY,
  user_id bigint NOT NULL REFERENCES users ON DELETE CASCADE,
  video_id text NOT NULL,
  title text,
  playlist_item_id text,
  added_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (user_id, video_id)
);
"""


def _role(name: str) -> str:
    if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", name):
        raise ValueError(f"bad role name: {name!r}")
    return name


def upgrade(app_role: str = "dwyt_app") -> None:
    role = _role(app_role)
    op.execute(SCHEMA)
    op.execute("ALTER TABLE watch_later ENABLE ROW LEVEL SECURITY")
    op.execute(f"CREATE POLICY own_rows ON watch_later USING (user_id = {ME}) WITH CHECK (user_id = {ME})")
    op.execute(f"GRANT SELECT ON watch_later TO {role}")  # changes go through the system role (watchlater.py)


def downgrade(app_role: str = "dwyt_app") -> None:
    op.execute("""
        DROP TABLE watch_later;
        ALTER TABLE users DROP COLUMN share_mode;
        ALTER TABLE youtube_links DROP COLUMN watch_playlist_id, DROP COLUMN can_write, DROP COLUMN subs_sync;
        ALTER TABLE channel_videos DROP COLUMN description;
        ALTER TABLE items DROP COLUMN read_at;
    """)
