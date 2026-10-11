"""Item-level changes in the user's library: the main transcript, removing a transcript, the
current summary, and the bulk backfill of missing steps. Everything runs in the user's own
session (row-level security); the shared results themselves are never changed here, only the
user's links to them (unlinked results are removed later by indexing.housekeeping)."""
from __future__ import annotations

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session as DbSession

from . import indexing, jobs, reuse
from .db import utcnow
from .models import (ACTIVE, Item, ItemSummary, ItemTranscript, ItemTranslation, Job, Summary, Transcript,
                     Translation)


def _title(item: Item) -> str | None:
    return (item.source.meta or {}).get("title") or item.source.url or item.title


def _active(s: DbSession, item_id: int, kind: str, transcript_id: int | None = None) -> list[Job]:
    found = s.scalars(select(Job).where(Job.item_id == item_id, Job.kind == kind, Job.state.in_(ACTIVE))).all()
    if transcript_id is None:
        return list(found)
    return [j for j in found if (j.payload or {}).get("transcript_id") == transcript_id]


def latest_summary(s: DbSession, item_id: int, transcript_id: int) -> int | None:
    return s.scalar(select(Summary.id).join(ItemSummary, ItemSummary.summary_id == Summary.id)
                    .where(ItemSummary.item_id == item_id, Summary.transcript_id == transcript_id)
                    .order_by(Summary.id.desc()).limit(1))


def latest_translation(s: DbSession, item_id: int, transcript_id: int) -> int | None:
    return s.scalar(select(Translation.id).join(ItemTranslation, ItemTranslation.translation_id == Translation.id)
                    .where(ItemTranslation.item_id == item_id, Translation.transcript_id == transcript_id)
                    .order_by(Translation.id.desc()).limit(1))


def set_main(s: DbSession, user_id: int, item: Item, transcript_id: int, want_summary: bool | None = None) -> bool:
    """Make one of the item's transcripts the main one. Its own summary and translation (if this
    item has them) become current; missing ones are queued: the summary when the item had one
    (or want_summary), the translation when it is not Hungarian and the user translates
    automatically. The search index follows."""
    if s.get(ItemTranscript, (item.id, transcript_id)) is None:
        return False
    if item.main_transcript_id == transcript_id:
        return True
    if want_summary is None:
        want_summary = item.current_summary_id is not None
    item.main_transcript_id = transcript_id
    item.current_summary_id = latest_summary(s, item.id, transcript_id)
    item.current_translation_id = latest_translation(s, item.id, transcript_id)
    item.updated_at = utcnow()
    s.flush()
    t = s.get(Transcript, transcript_id)
    if want_summary and item.current_summary_id is None and not _active(s, item.id, "summarize", transcript_id):
        jobs.enqueue_summary(s, user_id, item, transcript_id, _title(item))
    if (jobs.needs_translation(t.language) and item.current_translation_id is None
            and not _active(s, item.id, "translate", transcript_id) and reuse.translate_auto(user_id)):
        jobs.enqueue_translation(s, user_id, item, transcript_id, _title(item))
    indexing.enqueue(s, item)
    return True


def remove_transcript(s: DbSession, user_id: int, item: Item, transcript_id: int) -> bool:
    """Take a transcript (with its summaries and translations) out of the item. When it was the
    main one, the newest remaining transcript takes over."""
    if s.get(ItemTranscript, (item.id, transcript_id)) is None:
        return False
    was_main = item.main_transcript_id == transcript_id
    had_summary = item.current_summary_id is not None
    summary_ids = select(Summary.id).where(Summary.transcript_id == transcript_id)
    translation_ids = select(Translation.id).where(Translation.transcript_id == transcript_id)
    if was_main:
        item.main_transcript_id = item.current_summary_id = item.current_translation_id = None
    else:
        if item.current_summary_id and s.scalar(select(Summary.transcript_id).where(
                Summary.id == item.current_summary_id)) == transcript_id:
            item.current_summary_id = None
        if item.current_translation_id and s.scalar(select(Translation.transcript_id).where(
                Translation.id == item.current_translation_id)) == transcript_id:
            item.current_translation_id = None
    s.flush()
    s.execute(delete(ItemSummary).where(ItemSummary.item_id == item.id, ItemSummary.summary_id.in_(summary_ids)))
    s.execute(delete(ItemTranslation).where(ItemTranslation.item_id == item.id,
                                            ItemTranslation.translation_id.in_(translation_ids)))
    s.execute(delete(ItemTranscript).where(ItemTranscript.item_id == item.id,
                                           ItemTranscript.transcript_id == transcript_id))
    for kind in ("summarize", "translate"):
        for job in _active(s, item.id, kind, transcript_id):
            jobs.cancel(s, job)
    item.updated_at = utcnow()
    s.flush()
    if was_main:
        newest = s.scalar(select(ItemTranscript.transcript_id).where(ItemTranscript.item_id == item.id)
                          .order_by(ItemTranscript.transcript_id.desc()).limit(1))
        if newest is not None:
            set_main(s, user_id, item, newest, want_summary=had_summary)
    indexing.enqueue(s, item)
    return True


def set_current_summary(s: DbSession, item: Item, summary_id: int) -> bool:
    if s.get(ItemSummary, (item.id, summary_id)) is None:
        return False
    item.current_summary_id = summary_id
    item.updated_at = utcnow()
    indexing.enqueue(s, item)
    return True


def summary_versions(s: DbSession, item_id: int) -> list[tuple[Summary, bool]]:
    """(summary, reused) of the item, newest first."""
    rows = s.execute(select(Summary, ItemSummary.reused).join(ItemSummary, ItemSummary.summary_id == Summary.id)
                     .where(ItemSummary.item_id == item_id).order_by(Summary.id.desc())).all()
    return [(summary, reused) for summary, reused in rows]


# ---- bulk backfill ------------------------------------------------------------------------

def _busy(kind: str):
    return select(Job.item_id).where(Job.kind == kind, Job.state.in_(ACTIVE), Job.item_id.is_not(None))


def missing_summaries(s: DbSession) -> list[Item]:
    return list(s.scalars(select(Item).where(Item.main_transcript_id.is_not(None), Item.current_summary_id.is_(None),
                                             Item.id.not_in(_busy("summarize")))
                          .order_by(Item.created_at.desc())))


def missing_translations(s: DbSession) -> list[Item]:
    rows = s.execute(select(Item, Transcript.language).join(Transcript, Transcript.id == Item.main_transcript_id)
                     .where(Item.current_translation_id.is_(None), Item.id.not_in(_busy("translate")))
                     .order_by(Item.created_at.desc())).all()
    return [item for item, language in rows if jobs.needs_translation(language)]


def backfill_counts(s: DbSession) -> dict[str, int]:
    return {"summaries": len(missing_summaries(s)), "translations": len(missing_translations(s)),
            "all_items": s.scalar(select(func.count()).select_from(Item)) or 0}


def backfill(s: DbSession, user_id: int, what: str) -> int:
    """what: summaries | translations | index. Returns how many items were queued."""
    if what == "summaries":
        found = missing_summaries(s)
        for item in found:
            jobs.enqueue_summary(s, user_id, item, item.main_transcript_id, _title(item))
    elif what == "translations":
        found = missing_translations(s)
        for item in found:
            jobs.enqueue_translation(s, user_id, item, item.main_transcript_id, _title(item))
    elif what == "index":
        found = list(s.scalars(select(Item)))
        for item in found:
            indexing.enqueue(s, item)
    else:
        return 0
    return len(found)
