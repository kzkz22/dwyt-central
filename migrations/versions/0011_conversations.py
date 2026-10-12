"""Round 6: questions about an item or the whole library, answered by the user's runner (plan 15a).

Revision ID: 0011
Revises: 0010
"""
import re

from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None

ME = "current_setting('dwyt.user_id')::bigint"

SCHEMA = """
ALTER TABLE jobs DROP CONSTRAINT jobs_kind_check;
ALTER TABLE jobs ADD CONSTRAINT jobs_kind_check CHECK (kind IN ('transcribe', 'summarize', 'translate', 'ask'));

CREATE TABLE conversations (
  id bigserial PRIMARY KEY,
  user_id bigint NOT NULL REFERENCES users ON DELETE CASCADE,
  item_id bigint REFERENCES items ON DELETE CASCADE,          -- NULL: the whole library
  title text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX conversations_user ON conversations (user_id, updated_at DESC);
CREATE INDEX conversations_item ON conversations (item_id, updated_at DESC);

CREATE TABLE conversation_messages (
  id bigserial PRIMARY KEY,
  conversation_id bigint NOT NULL REFERENCES conversations ON DELETE CASCADE,
  user_id bigint NOT NULL REFERENCES users ON DELETE CASCADE,
  role text NOT NULL CHECK (role IN ('user', 'assistant')),
  content text NOT NULL,
  job_id bigint REFERENCES jobs ON DELETE SET NULL,           -- the question's job
  sources jsonb NOT NULL DEFAULT '[]',                        -- the background the question was answered from
  model text,
  model_used text,
  prompt_tokens integer,
  completion_tokens integer,
  reasoning_tokens integer,
  duration_s double precision,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX conversation_messages_conv ON conversation_messages (conversation_id, id);
"""


def _role(name: str) -> str:
    if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", name):
        raise ValueError(f"bad role name: {name!r}")
    return name


def upgrade(app_role: str = "dwyt_app") -> None:
    role = _role(app_role)
    op.execute(SCHEMA)
    for table in ("conversations", "conversation_messages"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"CREATE POLICY own_rows ON {table} USING (user_id = {ME}) WITH CHECK (user_id = {ME})")
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {role}")
        op.execute(f"GRANT USAGE, SELECT ON SEQUENCE {table}_id_seq TO {role}")


def downgrade(app_role: str = "dwyt_app") -> None:
    op.execute("""
        DROP TABLE conversation_messages; DROP TABLE conversations;
        DELETE FROM jobs WHERE kind = 'ask';
        ALTER TABLE jobs DROP CONSTRAINT jobs_kind_check;
        ALTER TABLE jobs ADD CONSTRAINT jobs_kind_check CHECK (kind IN ('transcribe', 'summarize', 'translate'));
    """)
