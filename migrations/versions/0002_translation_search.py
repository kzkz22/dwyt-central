"""Translations, search chunks (full text, trigrams, bge-m3 vectors), the index queue, the
per-user translation setting.

The extensions need a superuser once (docs/frissites.md); here they are only checked.

Revision ID: 0002
Revises: 0001
"""
import re

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

ME = "current_setting('dwyt.user_id')::bigint"

SCHEMA = """
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS unaccent;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- unaccent() is only STABLE; index expressions and stored tsvectors need an IMMUTABLE wrapper
-- with the dictionary named explicitly.
CREATE OR REPLACE FUNCTION dwyt_unaccent(text) RETURNS text
  LANGUAGE sql IMMUTABLE PARALLEL SAFE STRICT
  AS $$ SELECT public.unaccent('public.unaccent'::regdictionary, $1) $$;

ALTER TABLE users ADD COLUMN translate_auto boolean NOT NULL DEFAULT true;

-- shared, immutable result store (like summaries)
CREATE TABLE translations (
  id bigserial PRIMARY KEY,
  transcript_id bigint NOT NULL REFERENCES transcripts ON DELETE CASCADE,
  recipe_hash text NOT NULL,
  role text NOT NULL,
  language text NOT NULL,
  source_language text,
  model text,
  model_used text,
  prompt_version integer NOT NULL,
  segment_count integer NOT NULL DEFAULT 0,
  missing integer NOT NULL DEFAULT 0,
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
CREATE INDEX translations_recipe ON translations (recipe_hash);
CREATE INDEX translations_transcript ON translations (transcript_id);

CREATE TABLE translation_segments (
  translation_id bigint NOT NULL REFERENCES translations ON DELETE CASCADE,
  idx integer NOT NULL,
  text text NOT NULL,
  PRIMARY KEY (translation_id, idx)
);

CREATE TABLE item_translations (
  item_id bigint NOT NULL REFERENCES items ON DELETE CASCADE,
  translation_id bigint NOT NULL REFERENCES translations ON DELETE CASCADE,
  user_id bigint NOT NULL REFERENCES users ON DELETE CASCADE,
  reused boolean NOT NULL DEFAULT false,
  added_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (item_id, translation_id)
);
CREATE INDEX item_translations_user ON item_translations (user_id, translation_id);

ALTER TABLE items ADD COLUMN current_translation_id bigint REFERENCES translations ON DELETE SET NULL;

ALTER TABLE jobs DROP CONSTRAINT jobs_kind_check;
ALTER TABLE jobs ADD CONSTRAINT jobs_kind_check CHECK (kind IN ('transcribe', 'summarize', 'translate'));

-- search chunks, shared like the results they come from:
--   meta        per source (title, channel, URL); per item when item_id is set (own uploads)
--   transcript  1-2 minute stretches of a transcript, with the Hungarian translation (text_hu)
--   summary     a summary
CREATE TABLE chunks (
  id bigserial PRIMARY KEY,
  source_id bigint NOT NULL REFERENCES sources ON DELETE CASCADE,
  item_id bigint REFERENCES items ON DELETE CASCADE,
  transcript_id bigint REFERENCES transcripts ON DELETE CASCADE,
  translation_id bigint REFERENCES translations ON DELETE CASCADE,
  summary_id bigint REFERENCES summaries ON DELETE CASCADE,
  kind text NOT NULL CHECK (kind IN ('meta', 'transcript', 'summary')),
  seq integer NOT NULL,
  start_s double precision,
  end_s double precision,
  language text,
  text text NOT NULL,
  text_hu text,
  tsv tsvector NOT NULL,
  text_hash text NOT NULL,
  embedding vector(1024),
  embed_model text,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX chunks_source ON chunks (source_id);
CREATE INDEX chunks_transcript ON chunks (transcript_id, translation_id);
CREATE INDEX chunks_summary ON chunks (summary_id);
CREATE INDEX chunks_tsv ON chunks USING gin (tsv);
CREATE INDEX chunks_trgm ON chunks USING gin (lower(dwyt_unaccent(text || ' ' || coalesce(text_hu, ''))) gin_trgm_ops);
CREATE INDEX chunks_embedding ON chunks USING hnsw (embedding vector_cosine_ops);

-- items whose search chunks must be (re)built; served round-robin between users
CREATE TABLE index_queue (
  item_id bigint PRIMARY KEY REFERENCES items ON DELETE CASCADE,
  user_id bigint NOT NULL REFERENCES users ON DELETE CASCADE,
  queued_at timestamptz NOT NULL DEFAULT now(),
  run_after timestamptz NOT NULL DEFAULT now(),
  attempts integer NOT NULL DEFAULT 0,
  error text
);
CREATE INDEX index_queue_next ON index_queue (run_after, user_id, queued_at);

-- the items made before this version: index them
INSERT INTO index_queue (item_id, user_id) SELECT id, user_id FROM items;
"""

SHARED_POLICIES = {
    "translations": "EXISTS (SELECT 1 FROM item_translations it "
                    f"WHERE it.translation_id = translations.id AND it.user_id = {ME})",
    "translation_segments": "EXISTS (SELECT 1 FROM item_translations it "
                            f"WHERE it.translation_id = translation_segments.translation_id AND it.user_id = {ME})",
    "chunks": (
        f"(kind = 'meta' AND EXISTS (SELECT 1 FROM items i WHERE i.user_id = {ME} AND i.source_id = chunks.source_id "
        "AND (chunks.item_id IS NULL OR chunks.item_id = i.id))) "
        f"OR (kind = 'transcript' AND EXISTS (SELECT 1 FROM item_transcripts it WHERE it.user_id = {ME} "
        "AND it.transcript_id = chunks.transcript_id)) "
        f"OR (kind = 'summary' AND EXISTS (SELECT 1 FROM item_summaries s WHERE s.user_id = {ME} "
        "AND s.summary_id = chunks.summary_id))"),
}


def _role(name: str) -> str:
    if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", name):
        raise ValueError(f"bad role name: {name!r}")
    return name


def upgrade(app_role: str = "dwyt_app") -> None:
    role = _role(app_role)
    op.execute(SCHEMA)
    for table in ("item_translations", "index_queue"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"CREATE POLICY own_rows ON {table} USING (user_id = {ME}) WITH CHECK (user_id = {ME})")
    for table, cond in SHARED_POLICIES.items():
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"CREATE POLICY through_library ON {table} FOR SELECT USING ({cond})")
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON item_translations, index_queue TO {role}")
    op.execute(f"GRANT SELECT ON translations, translation_segments, chunks TO {role}")
    op.execute(f"GRANT EXECUTE ON FUNCTION dwyt_unaccent(text) TO {role}")


def downgrade(app_role: str = "dwyt_app") -> None:
    op.execute("""
        DROP TABLE index_queue, chunks;
        ALTER TABLE jobs DROP CONSTRAINT jobs_kind_check;
        ALTER TABLE jobs ADD CONSTRAINT jobs_kind_check CHECK (kind IN ('transcribe', 'summarize'));
        ALTER TABLE items DROP COLUMN current_translation_id;
        DROP TABLE item_translations, translation_segments, translations;
        ALTER TABLE users DROP COLUMN translate_auto;
        DROP FUNCTION IF EXISTS dwyt_unaccent(text);
    """)
