"""Search in the user's own library: one query box, three kinds of match fused into one ranking
(ported from yt-transcribe-webui).

  text      PostgreSQL full text search (stemmed, accent-insensitive; see indexing.py)
  partial   pg_trgm word similarity (threshold 0.4): word fragments and typos; with "exact
            only" this becomes a plain substring match
  meaning   bge-m3 vectors (cosine similarity >= SEARCH_MIN_SIMILARITY); off with "exact only"

The lists are fused with reciprocal rank fusion (1 / (60 + rank)); hits are grouped by item.
Every query runs in the user's session: row-level security limits the chunks and items to his
own, and the join limits the chunks to his items' current results.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from markupsafe import Markup, escape
from sqlalchemy import text

from . import db
from .config import Settings
from .embed_client import EmbedUnavailable, embed
from .indexing import ts_config

RRF_K = 60
PER_LIST = 120
MAX_ITEMS = 40
HITS_PER_ITEM = 3
BASE_CONFIGS = ("hungarian", "english", "simple")
TRGM_THRESHOLD = 0.4
HNSW_EF_SEARCH = 200


@dataclass
class Filters:
    channel: str = ""
    language: str = ""
    mode: str = ""      # local | cloud | captions (of the main transcript)
    date_from: date | None = None
    date_to: date | None = None

    def sql(self) -> tuple[str, dict[str, Any]]:
        where, params = [], {}
        if self.channel:
            where.append("src.meta->>'channel' = :f_channel")
            params["f_channel"] = self.channel
        if self.language:
            where.append("mt.language = :f_language")
            params["f_language"] = self.language
        if self.mode:
            where.append("mt.mode = :f_mode")
            params["f_mode"] = self.mode
        day = ("coalesce(CASE WHEN src.meta->>'published' ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$' "
               "THEN CAST(src.meta->>'published' AS date) END, i.created_at::date)")
        if self.date_from:
            where.append(f"{day} >= :f_from")
            params["f_from"] = self.date_from
        if self.date_to:
            where.append(f"{day} <= :f_to")
            params["f_to"] = self.date_to
        return (" AND " + " AND ".join(where)) if where else "", params


@dataclass
class Hit:
    chunk_id: int
    item_id: int
    kind: str
    start_s: float | None
    text: str
    text_hu: str | None
    language: str | None
    score: float = 0.0
    via: set[str] = field(default_factory=set)

    @property
    def literal(self) -> bool:
        return bool(self.via & {"text", "partial"})


@dataclass
class ItemResult:
    item: dict
    score: float
    hits: list[Hit]


@dataclass
class Outcome:
    results: list[ItemResult]
    meaning_used: bool
    meaning_error: str | None = None


_BASE = """
    FROM chunks c
    JOIN items i ON i.source_id = c.source_id AND (
           (c.kind = 'meta' AND (c.item_id IS NULL OR c.item_id = i.id))
        OR (c.kind = 'transcript' AND c.transcript_id = i.main_transcript_id
            AND c.translation_id IS NOT DISTINCT FROM i.current_translation_id)
        OR (c.kind = 'summary' AND c.summary_id = i.current_summary_id))
    JOIN sources src ON src.id = i.source_id
    LEFT JOIN transcripts mt ON mt.id = i.main_transcript_id
    WHERE {cond}{filters}
"""
_COLS = "c.id, i.id AS item_id, c.kind, c.start_s, c.text, c.text_hu, c.language"
_DOC = "lower(dwyt_unaccent(c.text || ' ' || coalesce(c.text_hu, '')))"


def fold(value: str) -> str:
    """Lower case without accents (the Python side of dwyt_unaccent + lower)."""
    return "".join(ch for ch in unicodedata.normalize("NFKD", value.lower()) if not unicodedata.combining(ch))


def _configs(s) -> list[str]:
    """Hungarian, English, simple, plus the stemmers of every language in the user's library:
    a German chunk is stored stemmed with the german config, so the query is stemmed the same way."""
    langs = s.scalars(text("SELECT DISTINCT t.language FROM items i JOIN transcripts t "
                           "ON t.id = i.main_transcript_id WHERE t.language IS NOT NULL")).all()
    return list(dict.fromkeys([*BASE_CONFIGS, *(ts_config(x) for x in langs)]))


def _tsquery_sql(configs: list[str]) -> str:
    parts = []
    for cfg in configs:  # names from our own table, never from the request
        parts.append(f"websearch_to_tsquery('{cfg}', :q)")
        parts.append(f"websearch_to_tsquery('{cfg}', dwyt_unaccent(:q))")
    return "(" + " || ".join(parts) + ")"


def run(user_id: int, q: str, settings: Settings, exact: bool = False, filters: Filters | None = None) -> Outcome:
    q = " ".join(q.split())[:300]
    filters = filters or Filters()
    if not q:
        return Outcome([], False)
    fsql, fparams = filters.sql()
    lists: dict[str, list] = {}
    with db.user_session(user_id) as s:
        tsq = _tsquery_sql(_configs(s))
        s.execute(text(f"SET LOCAL pg_trgm.word_similarity_threshold = {TRGM_THRESHOLD}"))
        lists["text"] = s.execute(text(
            f"SELECT {_COLS}, ts_rank_cd(c.tsv, {tsq}) AS r " + _BASE.format(cond=f"c.tsv @@ {tsq}", filters=fsql)
            + " ORDER BY r DESC LIMIT :n"), {"q": q, "n": PER_LIST, **fparams}).all()
        if exact:
            like = "%" + fold(q).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            lists["partial"] = s.execute(text(
                f"SELECT {_COLS}, 1.0 AS r " + _BASE.format(cond=f"{_DOC} LIKE :like", filters=fsql)
                + " ORDER BY i.id DESC LIMIT :n"), {"like": like, "n": PER_LIST, **fparams}).all()
        elif len(fold(q)) >= 3:
            lists["partial"] = s.execute(text(
                f"SELECT {_COLS}, word_similarity(:fq, {_DOC}) AS r "
                + _BASE.format(cond=f":fq <% {_DOC}", filters=fsql)
                + " ORDER BY r DESC LIMIT :n"), {"fq": fold(q), "n": PER_LIST, **fparams}).all()
    meaning_used, meaning_error = False, None
    if not exact and settings.embed_url:
        try:
            _model, [vector] = embed(settings.embed_url, [q], timeout=15)
            vec = "[" + ",".join(f"{x:.7g}" for x in vector) + "]"
            with db.user_session(user_id) as s:
                # the HNSW scan returns ef_search rows and filters apply after it: ask for more,
                # and on pgvector >= 0.8 keep scanning until enough pass
                s.execute(text(f"SET LOCAL hnsw.ef_search = {HNSW_EF_SEARCH}"))
                try:
                    with s.begin_nested():
                        s.execute(text("SET LOCAL hnsw.iterative_scan = relaxed_order"))
                except Exception:  # noqa: BLE001 - older pgvector
                    pass
                rows = s.execute(text(
                    f"SELECT {_COLS}, 1 - (c.embedding <=> CAST(:v AS vector)) AS r "
                    + _BASE.format(cond="c.embedding IS NOT NULL", filters=fsql)
                    + " ORDER BY c.embedding <=> CAST(:v AS vector) LIMIT :n"),
                    {"v": vec, "n": PER_LIST, **fparams}).all()
            lists["meaning"] = [row for row in rows if row.r >= settings.search_min_similarity]
            meaning_used = True
        except EmbedUnavailable as exc:
            meaning_error = str(exc)
    return Outcome(_fuse(user_id, lists), meaning_used, meaning_error)


def _fuse(user_id: int, lists: dict[str, list]) -> list[ItemResult]:
    hits: dict[tuple[int, int], Hit] = {}
    for name, rows in lists.items():
        for rank, row in enumerate(rows, 1):
            key = (row.id, row.item_id)
            hit = hits.get(key)
            if hit is None:
                hit = hits[key] = Hit(row.id, row.item_id, row.kind, row.start_s, row.text, row.text_hu, row.language)
            hit.score += 1.0 / (RRF_K + rank)
            hit.via.add(name)
    by_item: dict[int, list[Hit]] = {}
    for hit in sorted(hits.values(), key=lambda h: h.score, reverse=True):
        by_item.setdefault(hit.item_id, []).append(hit)
    ranked = sorted(by_item.items(), key=lambda kv: kv[1][0].score + 0.25 * sum(h.score for h in kv[1][1:]),
                    reverse=True)[:MAX_ITEMS]
    if not ranked:
        return []
    with db.user_session(user_id) as s:
        rows = s.execute(text(
            "SELECT i.id, coalesce(i.title, src.meta->>'title', src.url, src.key) AS title, src.kind, src.key, "
            "src.url, src.meta->>'channel' AS channel, src.meta->>'published' AS published, i.created_at, "
            "mt.language, mt.mode FROM items i JOIN sources src ON src.id = i.source_id "
            "LEFT JOIN transcripts mt ON mt.id = i.main_transcript_id WHERE i.id = ANY(:ids)"),
            {"ids": [k for k, _ in ranked]}).mappings().all()
    items = {r["id"]: dict(r) for r in rows}
    return [ItemResult(items[k], v[0].score, v[:HITS_PER_ITEM]) for k, v in ranked if k in items]


def channels(user_id: int) -> list[str]:
    with db.user_session(user_id) as s:
        return [c for c in s.scalars(text(
            "SELECT DISTINCT src.meta->>'channel' FROM items i JOIN sources src ON src.id = i.source_id "
            "WHERE src.meta->>'channel' IS NOT NULL ORDER BY 1")).all() if c]


# ---- snippets ---------------------------------------------------------------------------------

_WORD = re.compile(r"\w+", re.U)


def _terms(q: str) -> list[str]:
    """Query words as accent-free prefixes (so "kertről" also marks "kertje")."""
    out = []
    for w in _WORD.findall(fold(q)):
        if len(w) >= 3:
            out.append(w[: max(4, len(w) - 3)] if len(w) > 5 else w)
    return out


def snippet(value: str, q: str, width: int = 260) -> Markup:
    """A window of the text around the first query word, query words marked; HTML-escaped."""
    terms = _terms(q)
    folded = fold(value)
    if len(folded) != len(value):  # a character changed length when folded: skip marking
        folded = None
    first = None
    spans: list[tuple[int, int]] = []
    if folded is not None and terms:
        for m in _WORD.finditer(folded):
            if any(m.group(0).startswith(t) for t in terms):
                spans.append((m.start(), m.end()))
                first = m.start() if first is None else first
    start = max(0, (first or 0) - width // 3)
    if start:
        start = value.find(" ", start) + 1 or start
    end = min(len(value), start + width)
    if end < len(value):
        end = value.rfind(" ", start, end) if value.rfind(" ", start, end) > start else end
    out, pos = [], start
    for a, b in spans:
        if a < start or b > end:
            continue
        out.append(str(escape(value[pos:a])))
        out.append(f"<mark>{escape(value[a:b])}</mark>")
        pos = b
    out.append(str(escape(value[pos:end])))
    return Markup(("… " if start else "") + "".join(out) + (" …" if end < len(value) else ""))


def best_text(hit: Hit, q: str) -> tuple[str, str]:
    """(text to show, 'eredeti'|'magyar'): the side where the query words occur; Hungarian when
    the hit came by meaning only and a translation exists."""
    terms = _terms(q)
    if hit.kind == "summary":  # Markdown headings and bullets are noise in a snippet
        return re.sub(r"(?m)^\s*(#+|-)\s+", "", hit.text), "eredeti"
    if hit.text_hu:
        in_orig = any(t in fold(hit.text) for t in terms)
        in_hu = any(t in fold(hit.text_hu) for t in terms)
        if (in_hu and not in_orig) or (not hit.literal and not in_orig):
            return hit.text_hu, "magyar"
    return hit.text, "eredeti"
