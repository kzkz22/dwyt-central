"""Initial schema: users and sign-in, runners, shared result store, per-user library, jobs,
row-level security.

Revision ID: 0001
"""
import re

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

ME = "current_setting('dwyt.user_id')::bigint"

SCHEMA = """
CREATE TABLE users (
  id bigserial PRIMARY KEY,
  email text NOT NULL,
  google_sub text UNIQUE,
  name text,
  role text NOT NULL DEFAULT 'user' CHECK (role IN ('admin', 'user')),
  password_hash text,
  totp_secret text,
  totp_last_step bigint NOT NULL DEFAULT 0,
  failed_logins integer NOT NULL DEFAULT 0,
  locked_until timestamptz,
  disabled boolean NOT NULL DEFAULT false,
  created_at timestamptz NOT NULL DEFAULT now(),
  last_login_at timestamptz
);
CREATE UNIQUE INDEX users_email ON users (lower(email));

CREATE TABLE invites (
  id bigserial PRIMARY KEY,
  email text NOT NULL,
  token_hash text NOT NULL UNIQUE,
  created_by bigint REFERENCES users ON DELETE SET NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  expires_at timestamptz NOT NULL,
  used_at timestamptz,
  used_by bigint REFERENCES users ON DELETE SET NULL,
  revoked_at timestamptz
);

CREATE TABLE sessions (
  token_hash text PRIMARY KEY,
  user_id bigint NOT NULL REFERENCES users ON DELETE CASCADE,
  state text NOT NULL CHECK (state IN ('pending', 'active')),
  csrf_token text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  last_seen_at timestamptz NOT NULL DEFAULT now(),
  expires_at timestamptz NOT NULL,
  ip text,
  user_agent text
);

CREATE TABLE login_states (
  state_hash text PRIMARY KEY,
  nonce text NOT NULL,
  verifier text NOT NULL,
  invite_token_hash text,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE runners (
  id bigserial PRIMARY KEY,
  user_id bigint NOT NULL REFERENCES users ON DELETE CASCADE,
  name text NOT NULL,
  token_hash text NOT NULL UNIQUE,
  created_at timestamptz NOT NULL DEFAULT now(),
  last_seen_at timestamptz,
  version text,
  protocol integer,
  capabilities jsonb NOT NULL DEFAULT '{}',
  revoked_at timestamptz
);
CREATE INDEX runners_user ON runners (user_id);

CREATE TABLE pair_requests (
  id bigserial PRIMARY KEY,
  code text NOT NULL UNIQUE,
  poll_hash text NOT NULL UNIQUE,
  created_at timestamptz NOT NULL DEFAULT now(),
  expires_at timestamptz NOT NULL,
  user_id bigint REFERENCES users ON DELETE CASCADE,
  runner_name text,
  approved_at timestamptz
);

-- shared, immutable result store (reachable for a user only through his library rows)
CREATE TABLE sources (
  id bigserial PRIMARY KEY,
  kind text NOT NULL CHECK (kind IN ('youtube', 'url', 'upload')),
  key text NOT NULL UNIQUE,
  url text,
  meta jsonb NOT NULL DEFAULT '{}',
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE transcripts (
  id bigserial PRIMARY KEY,
  source_id bigint NOT NULL REFERENCES sources ON DELETE CASCADE,
  recipe_hash text NOT NULL,
  mode text NOT NULL CHECK (mode IN ('captions', 'cloud', 'local')),
  model text,
  caption_kind text,
  language text,
  diarized boolean NOT NULL DEFAULT false,
  speakers jsonb NOT NULL DEFAULT '[]',
  warnings jsonb NOT NULL DEFAULT '[]',
  cloud jsonb,
  options jsonb NOT NULL DEFAULT '{}',
  private boolean NOT NULL DEFAULT false,
  shareable boolean NOT NULL DEFAULT true,
  origin_user_id bigint REFERENCES users ON DELETE SET NULL,
  runner_version text,
  elapsed_s double precision,
  segment_count integer NOT NULL DEFAULT 0,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX transcripts_recipe ON transcripts (recipe_hash);
CREATE INDEX transcripts_source ON transcripts (source_id);

CREATE TABLE segments (
  transcript_id bigint NOT NULL REFERENCES transcripts ON DELETE CASCADE,
  idx integer NOT NULL,
  start_s double precision NOT NULL,
  end_s double precision NOT NULL,
  speaker text,
  text text NOT NULL,
  PRIMARY KEY (transcript_id, idx)
);

CREATE TABLE summaries (
  id bigserial PRIMARY KEY,
  transcript_id bigint NOT NULL REFERENCES transcripts ON DELETE CASCADE,
  recipe_hash text NOT NULL,
  role text NOT NULL,
  model text,
  model_used text,
  language text NOT NULL,
  prompt_version integer NOT NULL,
  text text NOT NULL,
  chunked boolean NOT NULL DEFAULT false,
  input_chars integer NOT NULL DEFAULT 0,
  prompt_tokens integer,
  completion_tokens integer,
  reasoning_tokens integer,
  duration_s double precision,
  private boolean NOT NULL DEFAULT false,
  shareable boolean NOT NULL DEFAULT true,
  origin_user_id bigint REFERENCES users ON DELETE SET NULL,
  runner_version text,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX summaries_recipe ON summaries (recipe_hash);
CREATE INDEX summaries_transcript ON summaries (transcript_id);

-- per-user library
CREATE TABLE items (
  id bigserial PRIMARY KEY,
  user_id bigint NOT NULL REFERENCES users ON DELETE CASCADE,
  source_id bigint NOT NULL REFERENCES sources,
  title text,
  is_public boolean NOT NULL,
  main_transcript_id bigint REFERENCES transcripts ON DELETE SET NULL,
  current_summary_id bigint REFERENCES summaries ON DELETE SET NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (user_id, source_id)
);
CREATE INDEX items_user ON items (user_id, created_at DESC);

CREATE TABLE item_transcripts (
  item_id bigint NOT NULL REFERENCES items ON DELETE CASCADE,
  transcript_id bigint NOT NULL REFERENCES transcripts ON DELETE CASCADE,
  user_id bigint NOT NULL REFERENCES users ON DELETE CASCADE,
  reused boolean NOT NULL DEFAULT false,
  added_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (item_id, transcript_id)
);
CREATE INDEX item_transcripts_user ON item_transcripts (user_id, transcript_id);

CREATE TABLE item_summaries (
  item_id bigint NOT NULL REFERENCES items ON DELETE CASCADE,
  summary_id bigint NOT NULL REFERENCES summaries ON DELETE CASCADE,
  user_id bigint NOT NULL REFERENCES users ON DELETE CASCADE,
  reused boolean NOT NULL DEFAULT false,
  added_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (item_id, summary_id)
);
CREATE INDEX item_summaries_user ON item_summaries (user_id, summary_id);

CREATE TABLE jobs (
  id bigserial PRIMARY KEY,
  user_id bigint NOT NULL REFERENCES users ON DELETE CASCADE,
  item_id bigint REFERENCES items ON DELETE CASCADE,
  kind text NOT NULL CHECK (kind IN ('transcribe', 'summarize')),
  state text NOT NULL DEFAULT 'queued' CHECK (state IN ('queued', 'running', 'done', 'failed', 'cancelled')),
  title text,
  payload jsonb NOT NULL DEFAULT '{}',
  runner_id bigint REFERENCES runners ON DELETE SET NULL,
  stage text,
  progress jsonb NOT NULL DEFAULT '{}',
  error text,
  attempts integer NOT NULL DEFAULT 0,
  run_after timestamptz NOT NULL DEFAULT now(),
  lease_until timestamptz,
  cancel_requested boolean NOT NULL DEFAULT false,
  created_at timestamptz NOT NULL DEFAULT now(),
  started_at timestamptz,
  finished_at timestamptz,
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX jobs_claim ON jobs (user_id, state, run_after);
"""

# own rows only
USER_TABLES = ("items", "item_transcripts", "item_summaries", "jobs", "runners")
# shared store: visible through the user's library rows
SHARED_POLICIES = {
    "transcripts": "EXISTS (SELECT 1 FROM item_transcripts it "
                   f"WHERE it.transcript_id = transcripts.id AND it.user_id = {ME})",
    "segments": "EXISTS (SELECT 1 FROM item_transcripts it "
                f"WHERE it.transcript_id = segments.transcript_id AND it.user_id = {ME})",
    "summaries": f"EXISTS (SELECT 1 FROM item_summaries s WHERE s.summary_id = summaries.id AND s.user_id = {ME})",
    "sources": f"EXISTS (SELECT 1 FROM items i WHERE i.source_id = sources.id AND i.user_id = {ME})",
}


def _role(name: str) -> str:
    if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", name):
        raise ValueError(f"bad role name: {name!r}")
    return name


def upgrade(app_role: str = "dwyt_app") -> None:
    role = _role(app_role)
    op.execute(SCHEMA)
    for table in USER_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"CREATE POLICY own_rows ON {table} USING (user_id = {ME}) WITH CHECK (user_id = {ME})")
    for table, cond in SHARED_POLICIES.items():
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"CREATE POLICY through_library ON {table} FOR SELECT USING ({cond})")
    op.execute(f"GRANT USAGE ON SCHEMA public TO {role}")
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON items, item_transcripts, item_summaries, jobs TO {role}")
    op.execute(f"GRANT SELECT, UPDATE ON runners TO {role}")
    op.execute(f"GRANT SELECT ON sources, transcripts, segments, summaries TO {role}")
    op.execute(f"GRANT USAGE, SELECT ON SEQUENCE items_id_seq, jobs_id_seq TO {role}")


def downgrade(app_role: str = "dwyt_app") -> None:
    op.execute("""DROP TABLE jobs, item_summaries, item_transcripts, items, summaries, segments, transcripts,
                  sources, pair_requests, runners, login_states, sessions, invites, users CASCADE""")
