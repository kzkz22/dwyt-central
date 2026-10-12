"""Export of the user's whole library (round 5) as a ZIP file: Markdown (one file per item) or
JSON (one file with everything). It contains what the user sees: the items with their tags,
note, current summary, main transcript (speaker names applied) and its Hungarian translation,
and the other transcripts' list. Read in the user's session (row-level security)."""
from __future__ import annotations

import json
import tempfile
import zipfile
from datetime import datetime
from typing import IO, Any

from sqlalchemy import select

from . import chat, db, items, library, personal
from .models import Conversation, ConversationMessage, Item, ItemTranscript, Summary, Transcript, Translation

FORMATS = ("md", "json")


def _item_data(s, item: Item) -> dict[str, Any]:
    meta = item.source.meta or {}
    transcripts = s.scalars(select(Transcript).join(ItemTranscript, ItemTranscript.transcript_id == Transcript.id)
                            .where(ItemTranscript.item_id == item.id).order_by(Transcript.id)).all()
    main = next((t for t in transcripts if t.id == item.main_transcript_id), None)
    summary = s.get(Summary, item.current_summary_id) if item.current_summary_id else None
    translation = None
    if main is not None:
        tid = item.current_translation_id or items.latest_translation(s, item.id, main.id)
        translation = s.get(Translation, tid) if tid else None
    hu = {g.idx: g.text for g in translation.segments} if translation else {}
    names = personal.names_for(s, item.id, main.id) if main else {}
    return {
        "item": item, "main": main, "summary": summary, "hu": hu, "names": names,
        "tags": personal.tags_of(s, item.id),
        "data": {
            "elem_id": item.id, "cim": library.item_title(item), "url": item.source.url,
            "forras": item.source.kind, "sajat_felvetel": not item.is_public, "csatorna": meta.get("channel"),
            "hossz_mp": meta.get("duration_s"), "megjelent": meta.get("published"),
            "hozzaadva": item.created_at.isoformat() if item.created_at else None,
            "cimkek": personal.tags_of(s, item.id), "jegyzet": item.note,
            "osszefoglalo": None if summary is None else {
                "szoveg": personal.rename_text(summary.text, personal.names_for(s, item.id, summary.transcript_id)),
                "modell": summary.model_used or summary.model, "keszult": summary.created_at.isoformat()},
            "atirat": None if main is None else {
                "atirat_id": main.id, "leiras": library.describe_transcript(main), "nyelv": main.language,
                "beszelok": [names.get(lb, lb) for lb in personal.labels_of(main)],
                "forditas_nyelve": translation.language if translation else None,
                "szegmensek": [{"kezdet": g.start_s, "veg": g.end_s, "beszelo": personal.rename(g.speaker, names),
                                "szoveg": g.text, **({"magyar": hu[g.idx]} if g.idx in hu else {})}
                               for g in main.segments]},
            "tobbi_atirat": [{"atirat_id": t.id, "leiras": library.describe_transcript(t), "nyelv": t.language}
                             for t in transcripts if main is None or t.id != main.id],
        },
    }


def _markdown(entry: dict[str, Any]) -> str:
    item, main, summary = entry["item"], entry["main"], entry["summary"]
    d = entry["data"]
    lines = [f"# {d['cim']}", ""]
    for label, value in (("URL", d["url"]), ("Csatorna", d["csatorna"]),
                         ("Hossz", library.hms(d["hossz_mp"]) if d["hossz_mp"] else None),
                         ("Hozzáadva", (d["hozzaadva"] or "")[:10]), ("Címkék", ", ".join(entry["tags"]) or None)):
        if value:
            lines.append(f"- {label}: {value}")
    if item.note:
        lines += ["", "## Jegyzet", "", item.note]
    if summary is not None:
        lines += ["", "## Összefoglaló", "", d["osszefoglalo"]["szoveg"].strip()]
    if main is not None:
        lines += ["", f"## Átirat ({library.describe_transcript(main)}, {main.language or '?'})", ""]
        for p in library.with_translation(library.turns(main.segments), entry["hu"] or None):
            speaker = personal.rename(p["speaker"], entry["names"])
            who = f" **{speaker}:**" if speaker else ""
            lines += [f"[{library.hms(p['start'])}]{who} {p['text']}", ""]
            if p.get("hu"):
                lines += [f"> {p['hu']}", ""]
    return "\n".join(lines).rstrip() + "\n"


def build(user_id: int, fmt: str) -> IO[bytes]:
    """The ZIP in a temporary file (kept in memory up to 32 MB), positioned at its start."""
    out = tempfile.SpooledTemporaryFile(max_size=32 * 1024 ** 2)
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as z, db.user_session(user_id) as s:
        all_items = s.scalars(select(Item).order_by(Item.created_at, Item.id)).all()
        everything = []
        for n, item in enumerate(all_items, 1):
            entry = _item_data(s, item)
            if fmt == "md":
                z.writestr(f"dwyt/{n:04d}-{library.file_base(item, 'elem')[:60]}.md", _markdown(entry))
            else:
                everything.append(entry["data"])
        talks = []
        titles = {i.id: library.item_title(i) for i in all_items}
        for conv in s.scalars(select(Conversation).order_by(Conversation.created_at, Conversation.id)):
            about = titles.get(conv.item_id, "elem") if conv.item_id else "a könyvtár"
            if fmt == "md":
                z.writestr(f"dwyt/beszelgetesek/{conv.id:05d}.md", chat.markdown(s, conv, about))
            else:
                talks.append({"cim": conv.title, "elem_id": conv.item_id, "kezdve": conv.created_at.isoformat(),
                              "uzenetek": [{"szerep": "kérdés" if m.role == "user" else "válasz", "szoveg": m.content,
                                            "modell": m.model_used or m.model, "ido": m.created_at.isoformat(),
                                            "forrasok": [{"sorszam": x.get("n"), "elem_id": x.get("item_id"),
                                                          "cim": x.get("title")} for x in m.sources or []]}
                                           for m in s.scalars(select(ConversationMessage).where(
                                               ConversationMessage.conversation_id == conv.id)
                                               .order_by(ConversationMessage.id))]})
        if fmt == "json":
            z.writestr("dwyt/konyvtar.json", json.dumps({"exportalva": datetime.now().isoformat(timespec="seconds"),
                                                         "elemek": everything, "beszelgetesek": talks},
                                                        ensure_ascii=False, indent=1))
    out.seek(0)
    return out
