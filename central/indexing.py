"""The search index: chunks with a text-search vector and a meaning vector (ported from
yt-transcribe-webui, made shared and multi-user).

Chunks belong to the shared results they come from, so a reused transcript is not embedded
again:
  meta        title, channel, URL of a source (own uploads: per item, file names stay private)
  transcript  1-2 minute stretches of a transcript, cut at a speaker change when long enough;
              built per (transcript, translation): text_hu carries the Hungarian translation
  summary     a summary
A user's search reaches the chunks of his items' current main transcript + translation and
current summary only (search.py joins on those pointers; row-level security on top).

Text search: a tsvector in the transcript's language (snowball stemming) and in Hungarian for
the translation, each both as written and without accents. Meaning: bge-m3 vectors of the
original text (it matches across languages); a chunk whose text did not change keeps its vector.

The queue (index_queue) holds items to (re)build; the indexer thread serves the users in turn
(the first waiting item of each user before anyone's second).
"""
from __future__ import annotations

import hashlib
import logging
import threading
import time
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import delete, func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session as DbSession

from . import db
from .config import get_settings
from .db import utcnow
from .embed_client import EmbedUnavailable, embed
from .models import IndexQueue, Item, Source, Summary, Transcript, Translation

log = logging.getLogger("dwyt.index")

TS_CONFIGS = {"hu": "hungarian", "en": "english", "de": "german", "fr": "french", "es": "spanish",
              "it": "italian", "pt": "portuguese", "nl": "dutch", "ru": "russian", "sv": "swedish",
              "fi": "finnish", "da": "danish", "no": "norwegian", "tr": "turkish"}
TARGET_S, MAX_S, MIN_S_AT_SPEAKER_CHANGE, MAX_CHARS = 90.0, 120.0, 45.0, 2500
EMBED_BATCH = 2  # small batches: a search query never waits long behind indexing
WEIGHTS = {"meta": "A", "summary": "B", "transcript": "C"}
RETRY_MINUTES = 5
HOUSEKEEPING_S = 3600


def ts_config(language: str | None) -> str:
    return TS_CONFIGS.get((language or "").split("-")[0].lower(), "simple")


def enqueue(s: DbSession, item: Item) -> None:
    """(Re)index the item soon; works in a user session (own rows) and the system session."""
    s.execute(insert(IndexQueue).values(item_id=item.id, user_id=item.user_id)
              .on_conflict_do_update(index_elements=[IndexQueue.item_id],
                                     set_={"queued_at": func.now(), "run_after": func.now(), "attempts": 0,
                                           "error": None}))


@dataclass
class Piece:
    kind: str
    seq: int
    text: str
    text_hu: str | None = None
    start_s: float | None = None
    end_s: float | None = None
    language: str | None = None

    @property
    def hash(self) -> str:
        return hashlib.sha256(self.text.encode()).hexdigest()


def transcript_pieces(t: Transcript, hu: dict[int, str] | None) -> list[Piece]:
    pieces: list[Piece] = []
    cur: list = []

    def flush() -> None:
        if cur:
            pieces.append(Piece(
                kind="transcript", seq=len(pieces) + 1, language=t.language,
                text=" ".join(s.text for s in cur).strip(),
                text_hu=(" ".join(hu[s.idx] for s in cur if hu.get(s.idx)).strip() or None) if hu else None,
                start_s=cur[0].start_s, end_s=cur[-1].end_s))
            cur.clear()

    for seg in t.segments:
        if cur:
            span = seg.end_s - cur[0].start_s
            chars = sum(len(s.text) for s in cur)
            speaker_change = (seg.speaker != cur[-1].speaker
                              and seg.start_s - cur[0].start_s >= MIN_S_AT_SPEAKER_CHANGE)
            if span > MAX_S or chars > MAX_CHARS or speaker_change or seg.start_s - cur[0].start_s >= TARGET_S:
                flush()
        cur.append(seg)
    flush()
    return [p for p in pieces if p.text]


def meta_text(src: Source) -> str:
    meta = src.meta or {}
    return "\n".join(x for x in (meta.get("title"), meta.get("channel"), src.url) if x) or src.key


_INSERT = text("""
    INSERT INTO chunks (source_id, item_id, transcript_id, translation_id, summary_id, kind, seq, start_s, end_s,
                        language, text, text_hu, text_hash, embedding, embed_model, tsv)
    VALUES (:source_id, :item_id, :transcript_id, :translation_id, :summary_id, :kind, :seq, :start_s, :end_s,
            :language, :text, :text_hu, :text_hash, CAST(:embedding AS vector), :embed_model,
            setweight(to_tsvector(CAST(:cfg AS regconfig), :text)
                      || to_tsvector(CAST(:cfg AS regconfig), dwyt_unaccent(:text)), CAST(:weight AS "char"))
            || setweight(to_tsvector('hungarian', coalesce(:text_hu, ''))
                      || to_tsvector('hungarian', dwyt_unaccent(coalesce(:text_hu, ''))), CAST(:weight AS "char")))
""")


def _insert(s: DbSession, p: Piece, *, source_id: int, item_id: int | None = None, transcript_id: int | None = None,
            translation_id: int | None = None, summary_id: int | None = None,
            reuse: tuple[str, str] | None = None) -> None:
    s.execute(_INSERT, {
        "source_id": source_id, "item_id": item_id, "transcript_id": transcript_id,
        "translation_id": translation_id, "summary_id": summary_id, "kind": p.kind, "seq": p.seq,
        "start_s": p.start_s, "end_s": p.end_s, "language": p.language, "text": p.text, "text_hu": p.text_hu,
        "text_hash": p.hash, "embedding": reuse[0] if reuse else None, "embed_model": reuse[1] if reuse else None,
        "cfg": ts_config(p.language if p.kind != "meta" else None), "weight": WEIGHTS[p.kind]})


def _old_vectors(s: DbSession, where: str, params: dict) -> dict[str, tuple[str, str]]:
    rows = s.execute(text(f"SELECT text_hash, embedding::text, embed_model FROM chunks WHERE {where} "
                          "AND embedding IS NOT NULL"), params).all()
    return {h: (e, m) for h, e, m in rows}


def build(s: DbSession, item: Item) -> None:
    """Make sure the chunks of the item's current results exist (shared; built once)."""
    src = s.get(Source, item.source_id)
    own = src.kind == "upload"  # file names of own recordings are not shared
    meta = Piece(kind="meta", seq=0, text=(item.title or "saját felvétel") if own else meta_text(src))
    cond = "source_id = :sid AND kind = 'meta' AND " + ("item_id = :iid" if own else "item_id IS NULL")
    params = {"sid": src.id, "iid": item.id}
    current = s.execute(text(f"SELECT text_hash FROM chunks WHERE {cond}"), params).scalars().all()
    if current != [meta.hash]:
        reuse = _old_vectors(s, cond, params).get(meta.hash)
        s.execute(text(f"DELETE FROM chunks WHERE {cond}"), params)
        _insert(s, meta, source_id=src.id, item_id=item.id if own else None, reuse=reuse)

    if item.main_transcript_id:
        tid, trid = item.main_transcript_id, item.current_translation_id
        exists = s.execute(text("SELECT 1 FROM chunks WHERE kind = 'transcript' AND transcript_id = :t "
                                "AND translation_id IS NOT DISTINCT FROM :tr LIMIT 1"), {"t": tid, "tr": trid}).first()
        t = s.get(Transcript, tid)
        if exists is None and t is not None:
            tr = s.get(Translation, trid) if trid else None
            hu = {g.idx: g.text for g in tr.segments} if tr else None
            old = _old_vectors(s, "kind = 'transcript' AND transcript_id = :t", {"t": tid})
            for p in transcript_pieces(t, hu):
                _insert(s, p, source_id=src.id, transcript_id=tid, translation_id=trid, reuse=old.get(p.hash))

    if item.current_summary_id:
        exists = s.execute(text("SELECT 1 FROM chunks WHERE kind = 'summary' AND summary_id = :sid LIMIT 1"),
                           {"sid": item.current_summary_id}).first()
        summary = s.get(Summary, item.current_summary_id)
        if exists is None and summary is not None:
            _insert(s, Piece(kind="summary", seq=0, text=summary.text, language=summary.language),
                    source_id=src.id, summary_id=summary.id)


_CURRENT = """
    FROM chunks c JOIN items i ON i.source_id = c.source_id AND i.id = :iid AND (
        (c.kind = 'meta' AND (c.item_id IS NULL OR c.item_id = i.id))
     OR (c.kind = 'transcript' AND c.transcript_id = i.main_transcript_id
         AND c.translation_id IS NOT DISTINCT FROM i.current_translation_id)
     OR (c.kind = 'summary' AND c.summary_id = i.current_summary_id))
"""


def embed_missing(item_id: int, url: str) -> int:
    """Fill in the meaning vectors the item's current chunks lack (or made by another model)."""
    model_name = get_settings().embed_model
    with db.system_session() as s:
        todo = s.execute(text(f"SELECT c.id, c.text {_CURRENT} WHERE c.embedding IS NULL "
                              "OR c.embed_model IS DISTINCT FROM :m ORDER BY c.id"),
                         {"iid": item_id, "m": model_name}).all()
    done = 0
    for i in range(0, len(todo), EMBED_BATCH):
        batch = todo[i:i + EMBED_BATCH]
        model, vectors = embed(url, [t for _id, t in batch])
        with db.system_session() as s:
            for (chunk_id, _t), vector in zip(batch, vectors, strict=True):
                s.execute(text("UPDATE chunks SET embedding = CAST(:v AS vector), embed_model = :m WHERE id = :id"),
                          {"v": "[" + ",".join(f"{float(x):.7g}" for x in vector) + "]", "m": model, "id": chunk_id})
        done += len(batch)
    return done


_last_served: dict[int, float] = {}  # user id -> when one of his items was last taken (one process)


def next_item() -> int | None:
    """The oldest waiting item of the user who was served longest ago (round robin): one user's
    big backlog never makes the others wait."""
    with db.system_session() as s:
        rows = s.execute(text("""
            SELECT DISTINCT ON (user_id) user_id, item_id, queued_at FROM index_queue
            WHERE run_after <= now() ORDER BY user_id, queued_at, item_id""")).all()
    if not rows:
        return None
    user_id, item_id, _ = min(rows, key=lambda r: (_last_served.get(r.user_id, 0.0), r.queued_at, r.item_id))
    _last_served[user_id] = time.monotonic()
    return item_id


def process(item_id: int) -> bool:
    """Build and embed one item. True when it is done (also when the item is gone)."""
    settings = get_settings()
    with db.system_session() as s:
        queued = s.scalar(select(IndexQueue).where(IndexQueue.item_id == item_id).with_for_update(skip_locked=True))
        if queued is None:
            return True
        item = s.get(Item, item_id)
        if item is None:
            s.delete(queued)
            return True
        build(s, item)
    try:
        if settings.embed_url:
            embed_missing(item_id, settings.embed_url)
    except EmbedUnavailable as exc:
        with db.system_session() as s:
            q = s.get(IndexQueue, item_id)
            if q is not None:
                q.attempts += 1
                q.error = str(exc)[:500]
                q.run_after = utcnow() + timedelta(minutes=RETRY_MINUTES)
        log.warning("item %s: text search ready, meaning vectors later: %s", item_id, exc)
        return False
    with db.system_session() as s:
        s.execute(delete(IndexQueue).where(IndexQueue.item_id == item_id))
    return True


def run_pending(limit: int = 1000) -> int:
    """Process waiting items (tests and the indexer thread); returns how many were handled."""
    handled = 0
    while handled < limit:
        item_id = next_item()
        if item_id is None:
            break
        process(item_id)
        handled += 1
    return handled


def housekeeping() -> None:
    """Drop chunks that no item points at any more (an old translation, a replaced summary...)."""
    with db.system_session() as s:
        s.execute(text("""DELETE FROM chunks c WHERE c.kind = 'transcript' AND NOT EXISTS (
            SELECT 1 FROM items i WHERE i.main_transcript_id = c.transcript_id
            AND i.current_translation_id IS NOT DISTINCT FROM c.translation_id)"""))
        s.execute(text("""DELETE FROM chunks c WHERE c.kind = 'summary' AND NOT EXISTS (
            SELECT 1 FROM items i WHERE i.current_summary_id = c.summary_id)"""))
        s.execute(text("""DELETE FROM chunks c WHERE c.kind = 'meta' AND NOT EXISTS (
            SELECT 1 FROM items i WHERE i.source_id = c.source_id AND (c.item_id IS NULL OR c.item_id = i.id))"""))


def status(user_id: int) -> dict:
    """How many of the user's items wait for indexing (for the search page)."""
    with db.user_session(user_id) as s:
        waiting = s.scalar(select(func.count()).select_from(IndexQueue)) or 0
        failing = s.scalar(select(IndexQueue.error).where(IndexQueue.error.is_not(None)).limit(1))
    return {"waiting": waiting, "error": failing}


class Indexer(threading.Thread):
    """Runs inside the web service (one process): builds chunks, asks for vectors, cleans up."""

    def __init__(self) -> None:
        super().__init__(daemon=True, name="indexer")
        self.stop = threading.Event()
        self._last_housekeeping = 0.0

    def run(self) -> None:
        log.info("indexer started")
        while not self.stop.is_set():
            try:
                if time.monotonic() - self._last_housekeeping > HOUSEKEEPING_S:
                    housekeeping()
                    from . import ownfiles
                    ownfiles.housekeeping(get_settings())
                    self._last_housekeeping = time.monotonic()
                item_id = next_item()
                if item_id is None:
                    self.stop.wait(2)
                    continue
                process(item_id)
            except Exception:  # noqa: BLE001 - keep the thread alive
                log.exception("indexer error")
                self.stop.wait(10)
