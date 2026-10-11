"""The user's own additions to an item (round 5): tags, a note, names for a transcript's speakers.
They live in the user's rows (row-level security); the shared results are never changed, so the
names are applied when a transcript or a summary is shown, downloaded or exported."""
from __future__ import annotations

import re

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session as DbSession

from .models import Item, ItemTag, ItemTranscript, Transcript

MAX_TAGS = 20
TAG_LEN = 40
NOTE_LEN = 20000
NAME_LEN = 60


def parse_tags(text: str) -> list[str]:
    """Comma-separated tags: trimmed, inner spaces collapsed, duplicates (any case) dropped."""
    out: list[str] = []
    seen: set[str] = set()
    for part in (text or "").replace(";", ",").split(","):
        tag = " ".join(part.split())[:TAG_LEN].strip().lstrip("#")
        if tag and tag.lower() not in seen:
            seen.add(tag.lower())
            out.append(tag)
    return out[:MAX_TAGS]


def tags_of(s: DbSession, item_id: int) -> list[str]:
    return list(s.scalars(select(ItemTag.tag).where(ItemTag.item_id == item_id).order_by(func.lower(ItemTag.tag))))


def tags_by_item(s: DbSession, item_ids: list[int]) -> dict[int, list[str]]:
    out: dict[int, list[str]] = {}
    for item_id, tag in s.execute(select(ItemTag.item_id, ItemTag.tag).where(ItemTag.item_id.in_(item_ids or [0]))
                                  .order_by(func.lower(ItemTag.tag))):
        out.setdefault(item_id, []).append(tag)
    return out


def all_tags(s: DbSession) -> list[tuple[str, int]]:
    """The user's tags with their counts, alphabetically."""
    return [(t, n) for t, n in s.execute(select(ItemTag.tag, func.count()).group_by(ItemTag.tag)
                                         .order_by(func.lower(ItemTag.tag)))]


def set_tags(s: DbSession, item: Item, tags: list[str]) -> None:
    s.execute(delete(ItemTag).where(ItemTag.item_id == item.id))
    s.add_all(ItemTag(item_id=item.id, tag=tag, user_id=item.user_id) for tag in tags)


def set_note(item: Item, text: str) -> None:
    text = (text or "").replace("\r\n", "\n").strip()[:NOTE_LEN]
    item.note = text or None


# ---- speaker names ------------------------------------------------------------------------

def names_for(s: DbSession, item_id: int, transcript_id: int | None) -> dict[str, str]:
    if not transcript_id:
        return {}
    names = s.scalar(select(ItemTranscript.speaker_names).where(ItemTranscript.item_id == item_id,
                                                                ItemTranscript.transcript_id == transcript_id))
    return {str(k): str(v) for k, v in (names or {}).items() if v}


def labels_of(t: Transcript) -> list[str]:
    """The transcript's speaker labels in order of first appearance."""
    labels = [str(sp.get("label")) for sp in (t.speakers or []) if sp.get("label")]
    for seg in t.segments:
        if seg.speaker and seg.speaker not in labels:
            labels.append(seg.speaker)
    return labels


def set_names(s: DbSession, item_id: int, t: Transcript, given: dict[str, str]) -> dict[str, str]:
    labels = set(labels_of(t))
    names = {label: " ".join(name.split())[:NAME_LEN] for label, name in given.items()
             if label in labels and name and name.strip()}
    link = s.get(ItemTranscript, (item_id, t.id))
    if link is not None:
        link.speaker_names = names
    return names


def rename(label: str | None, names: dict[str, str]) -> str | None:
    return names.get(label, label) if label else label


def rename_text(text: str, names: dict[str, str]) -> str:
    """Speaker labels in free text (a summary) replaced, the longest first ('Szereplő 10' before 'Szereplő 1')."""
    for label in sorted(names, key=len, reverse=True):
        text = re.sub(rf"(?<!\w){re.escape(label)}(?!\w)", names[label], text)
    return text
