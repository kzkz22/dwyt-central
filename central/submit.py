"""Submitting work: options from a form (or an MCP call), a new item for a URL, and a new
transcript of an existing item. Shared by the web pages and the MCP tools."""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from . import db, indexing, items, jobs, ownfiles, reuse, sources
from .config import get_settings
from .models import ACTIVE, Item, ItemSummary, ItemTranscript, ItemTranslation, Job, Source

MODES = {"captions": "YouTube-felirat (gyors, ingyenes, beszélők nélkül)",
         "local": "Helyi Whisper (a saját GPU-don, beszélőkkel; megvárja a GPU-gépet)",
         "cloud": "Felhős átírás (a futtatód AssemblyAI-kulcsával, beszélőkkel)"}
CLOUD_MODELS = ("auto", "universal-2", "universal-3-5-pro")
LOCAL_MODELS = ("large-v3", "large-v3-turbo")


def parse_options(form: dict[str, str]) -> tuple[dict[str, Any], list[str]]:
    errors: list[str] = []
    mode = form.get("mode") or "captions"
    if mode not in MODES:
        errors.append("Ismeretlen mód.")
    language = (form.get("language") or "").strip().lower() or None
    if language and not (2 <= len(language) <= 3 and language.isalpha()):
        errors.append("A nyelv kétbetűs kód legyen (például hu, en), vagy üres az automatikus felismeréshez.")
    options: dict[str, Any] = {"mode": mode, "language": language, "diarize": mode != "captions"}
    if mode == "cloud":
        model = form.get("model_cloud") or "auto"
        if model not in CLOUD_MODELS:
            errors.append("Ismeretlen felhős modell.")
        options["model"] = model
    if mode == "local":
        model = form.get("model_local") or LOCAL_MODELS[0]
        if model not in LOCAL_MODELS:
            errors.append("Ismeretlen Whisper-modell.")
        options["model"] = model
    if mode in ("cloud", "local"):
        _speakers(form, options, errors)
    return options, errors


SPEAKER_MODES = ("auto", "exact", "range", "none")


def _count(value: str | None, label: str, errors: list[str]) -> int | None:
    value = (value or "").strip()
    if not value:
        return None
    if not value.isdigit() or not 1 <= int(value) <= 20:
        errors.append(f"{label}: 1 és 20 közötti szám legyen.")
        return None
    return int(value)


def _speakers(form: dict[str, str], options: dict[str, Any], errors: list[str]) -> None:
    """auto | exact (num_speakers) | range (min/max) | none (no diarization). A lone num_speakers means exact."""
    mode = form.get("speakers") or ("exact" if (form.get("num_speakers") or "").strip() else "auto")
    if mode not in SPEAKER_MODES:
        errors.append("Ismeretlen beszélő-beállítás.")
        return
    if mode == "none":
        options["diarize"] = False
    elif mode == "exact":
        count = _count(form.get("num_speakers"), "A beszélők száma", errors)
        if count is None and not errors:
            errors.append("Add meg a beszélők számát.")
        if count:
            options["num_speakers"] = count
    elif mode == "range":
        low = _count(form.get("min_speakers"), "Legalább", errors)
        high = _count(form.get("max_speakers"), "Legfeljebb", errors)
        if low is None and high is None and not errors:
            errors.append("Adj meg legalább egy határt a beszélők számára.")
        if low and high and low > high:
            errors.append("A legkisebb beszélőszám nem lehet nagyobb a legnagyobbnál.")
        if low:
            options["min_speakers"] = low
        if high:
            options["max_speakers"] = high


def source_for(ref: sources.SourceRef) -> int:
    with db.system_session() as s:
        s.execute(insert(Source).values(kind=ref.kind, key=ref.key, url=ref.url, meta={})
                  .on_conflict_do_nothing(index_elements=[Source.key]))
        return s.scalar(select(Source.id).where(Source.key == ref.key))


def submit_url(user_id: int, ref: sources.SourceRef, options: dict[str, Any], summarize: bool) -> int:
    """The item of this source (new or existing) with a reused result or a queued transcription."""
    source_id = source_for(ref)
    with db.system_session() as s:
        source = s.get(Source, source_id)
        s.expunge(source)
    found = reuse.find(source, options)
    auto_translate = reuse.translate_auto(user_id)
    with db.user_session(user_id) as s:
        item = s.scalar(select(Item).where(Item.source_id == source_id))
        if item is None:
            item = Item(user_id=user_id, source_id=source_id, is_public=True)
            s.add(item)
            s.flush()
            indexing.enqueue(s, item)  # title and channel are searchable at once
        if found is not None:
            reuse.attach(s, user_id, item, found, summarize, auto_translate)
        else:
            jobs.enqueue_transcription(s, user_id, item, s.get(Source, source_id), options, summarize)
        return item.id


def own_file_of(s, item: Item) -> dict[str, Any] | None:
    """The watched-folder file of an own recording (the runner that registered it still has it);
    browser uploads are gone from here once transcribed."""
    for job in s.scalars(select(Job).where(Job.item_id == item.id, Job.kind == "transcribe")
                         .order_by(Job.id.desc())):
        src = (job.payload or {}).get("source") or {}
        if src.get("kind") == "local":
            return dict(src)
    return None


def retranscribe(user_id: int, item_id: int, options: dict[str, Any], make_main: bool, summarize: bool) -> str | None:
    """A new transcript of the item with other settings; returns a user-facing error or None."""
    with db.system_session() as s:
        item_source = s.scalar(select(Source).join(Item, Item.source_id == Source.id)
                               .where(Item.id == item_id, Item.user_id == user_id))
        if item_source is not None:
            s.expunge(item_source)
    if item_source is None:
        return "Nincs ilyen elem."
    if options.get("mode") == "captions" and item_source.kind != "youtube":
        return "YouTube-feliratot csak YouTube-videóhoz lehet kérni."
    try:
        ownfiles.check_storage(get_settings(), user_id)
    except ownfiles.QuotaError as exc:
        return str(exc)
    found = reuse.find(item_source, options)
    with db.user_session(user_id) as s:
        item = s.get(Item, item_id)
        busy = s.scalar(select(Job.id).where(Job.item_id == item.id, Job.kind == "transcribe",
                                             Job.state.in_(ACTIVE)))
        if busy:
            return "Az elemhez már fut egy átírás; várd meg, vagy szakítsd meg a Munkák oldalon."
        if found is not None:
            if s.get(ItemTranscript, (item.id, found.transcript_id)) is not None:
                return "Ilyen beállításokkal már van átirata az elemnek."
            s.add(ItemTranscript(item_id=item.id, transcript_id=found.transcript_id, user_id=user_id,
                                 reused=True))
            for model, key, ident in ((ItemSummary, "summary_id", found.summary_id),
                                      (ItemTranslation, "translation_id", found.translation_id)):
                if ident and s.get(model, (item.id, ident)) is None:
                    s.add(model(item_id=item.id, user_id=user_id, reused=True, **{key: ident}))
            s.flush()
            if make_main or item.main_transcript_id is None:
                items.set_main(s, user_id, item, found.transcript_id, want_summary=summarize)
            return None
        file = None
        if item_source.kind == "upload":
            file = own_file_of(s, item)
            if file is None:
                return ("A feltöltött fájl már nincs a központon. Töltsd fel újra az Új átirat oldalon: "
                        "az új átirat ehhez az elemhez kerül.")
            if options.get("mode") == "captions":
                return "Saját felvételhez YouTube-felirat nem kérhető."
        job = jobs.enqueue_transcription(s, user_id, item, s.get(Source, item_source.id), options, summarize,
                                         file=file)
        if make_main:
            job.payload = {**job.payload, "make_main": True}
        return None
