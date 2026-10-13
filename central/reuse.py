"""Reuse of finished results (plan 7, decision 13.3).

At submit, a transcript made by anyone on the same source with the same settings (mode, model,
language, speaker options) is handed over at once instead of sending a job to a runner. Its
summary and Hungarian translation come along when they exist. The user sees "átvett" (taken
over), never whose it was. Results of own recordings (uploads) are never shared.

The lookup crosses users, so it runs in the system session, in this narrow module only; the
links are then written in the user's own session (row-level security).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from . import captionpolicy, db, indexing, jobs
from .models import (Item, ItemSummary, ItemTranscript, ItemTranslation, Source, Summary, Transcript, Translation,
                     User)


@dataclass
class Found:
    transcript_id: int
    language: str | None
    summary_id: int | None
    translation_id: int | None


def find(source: Source, options: dict, captions_policy: Any = None) -> Found | None:
    """A shareable transcript (and its newest shareable summary and translation) for this recipe.
    captions_policy: a captions transcript is taken over only if the user's caption rule accepts it."""
    if source.kind == "upload":
        return None
    recipe = jobs.recipe_hash({"source": source.key, "options": options})
    with db.system_session() as s:
        t = s.scalar(select(Transcript).where(Transcript.recipe_hash == recipe, Transcript.shareable.is_(True),
                                              Transcript.private.is_(False)).order_by(Transcript.id.desc()))
        if t is None:
            return None
        if t.mode == "captions" and not captionpolicy.accepts(captions_policy, t.caption_kind, t.language):
            return None
        summary_id = s.scalar(select(Summary.id).where(Summary.transcript_id == t.id, Summary.shareable.is_(True),
                                                       Summary.private.is_(False))
                              .order_by(Summary.prompt_version.desc(), Summary.id.desc()))
        translation_id = s.scalar(select(Translation.id).where(
            Translation.transcript_id == t.id, Translation.shareable.is_(True), Translation.private.is_(False),
            Translation.language == jobs.TRANSLATION_TARGET)
            .order_by(Translation.prompt_version.desc(), Translation.missing, Translation.id.desc()))
        return Found(t.id, t.language, summary_id, translation_id)


def translate_auto(user_id: int) -> bool:
    with db.system_session() as s:
        user = s.get(User, user_id)
        return user is None or user.translate_auto


def attach(s: DbSession, user_id: int, item: Item, found: Found, summarize: bool, auto_translate: bool) -> None:
    """Link the found results to the user's item (user session) and queue what is still missing."""
    if s.get(ItemTranscript, (item.id, found.transcript_id)) is None:
        s.add(ItemTranscript(item_id=item.id, transcript_id=found.transcript_id, user_id=user_id, reused=True))
    if item.main_transcript_id is None:
        item.main_transcript_id = found.transcript_id
    s.flush()
    if item.main_transcript_id != found.transcript_id:
        return  # the item already had its own main transcript; the reused one is just listed
    title = (item.source.meta or {}).get("title") or item.source.url
    if summarize and item.current_summary_id is None:
        if found.summary_id:
            if s.get(ItemSummary, (item.id, found.summary_id)) is None:
                s.add(ItemSummary(item_id=item.id, summary_id=found.summary_id, user_id=user_id, reused=True))
            item.current_summary_id = found.summary_id
        else:
            jobs.enqueue_summary(s, user_id, item, found.transcript_id, title)
    if jobs.needs_translation(found.language) and item.current_translation_id is None:
        if found.translation_id:
            if s.get(ItemTranslation, (item.id, found.translation_id)) is None:
                s.add(ItemTranslation(item_id=item.id, translation_id=found.translation_id, user_id=user_id,
                                      reused=True))
            item.current_translation_id = found.translation_id
        elif auto_translate:
            jobs.enqueue_translation(s, user_id, item, found.transcript_id, title)
    indexing.enqueue(s, item)
