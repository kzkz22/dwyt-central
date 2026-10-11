"""The job queue between the library and the runners (protocol: dwyt-docs/docs/protokoll.md).

Users enqueue jobs in their own session (row-level security applies). Runners claim, renew,
finish and fail them through the protocol, which runs as the system role but always limited
to the runner's own user. Results go into the shared result store and are linked to the
user's item.
"""
from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session as DbSession

from . import indexing, sources
from .config import Settings
from .db import utcnow
from .models import (Item, ItemSummary, ItemTranscript, ItemTranslation, Job, Runner, Segment, Source, Summary,
                     Transcript, Translation, TranslationSegment, User)

RETRY_MINUTES = (1, 5, 15, 60, 180, 360)
STAGE_LABELS = {
    None: "", "queued": "Sorban áll", "download": "Letöltés", "captions": "Felirat letöltése",
    "prepare-audio": "Hang előkészítése", "upload": "Feltöltés a felhőbe", "transcribe": "Átírás",
    "summarize": "Összefoglaló írása", "write": "Mentés", "starting": "Indul",
    "waiting-for-gpu": "Vár a GPU-ra", "load-audio": "Hang betöltése", "align": "Igazítás",
    "diarize": "Beszélők szétválasztása", "translate": "Fordítás",
}
KIND_LABELS = {"transcribe": "Átírás", "summarize": "Összefoglaló", "translate": "Fordítás"}
TRANSLATION_TARGET = "hu"
STATE_LABELS = {"queued": "Sorban", "running": "Fut", "done": "Kész", "failed": "Hiba", "cancelled": "Megszakítva"}
CAPTIONS_SUMMARY_LANGUAGE = "hu"


class ProtocolError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def stage_label(stage: str | None) -> str:
    return STAGE_LABELS.get(stage, stage or "")


def recipe_hash(data: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


# ---- enqueueing (user session) ------------------------------------------------------------

def enqueue_transcription(s: DbSession, user_id: int, item: Item, source: Source, options: dict[str, Any],
                          summarize: bool) -> Job:
    payload = {"source": {"kind": source.kind, "url": source.url,
                          "video_id": source.key.split(":", 1)[1] if source.kind == "youtube" else None},
               "options": options, "summarize": summarize}
    job = Job(user_id=user_id, item_id=item.id, kind="transcribe", state="queued", stage="queued",
              title=(source.meta or {}).get("title") or source.url, payload=payload, progress={})
    s.add(job)
    s.flush()
    return job


def needs_translation(language: str | None) -> bool:
    return bool(language) and not language.lower().startswith(TRANSLATION_TARGET)


def enqueue_translation(s: DbSession, user_id: int, item: Item, transcript_id: int, title: str | None) -> Job:
    payload = {"transcript_id": transcript_id, "role": "public" if item.is_public else "private",
               "target": TRANSLATION_TARGET}
    job = Job(user_id=user_id, item_id=item.id, kind="translate", state="queued", stage="queued",
              title=title, payload=payload, progress={})
    s.add(job)
    s.flush()
    return job


def enqueue_summary(s: DbSession, user_id: int, item: Item, transcript_id: int, title: str | None) -> Job:
    payload = {"transcript_id": transcript_id, "role": "public" if item.is_public else "private",
               "language": CAPTIONS_SUMMARY_LANGUAGE}
    job = Job(user_id=user_id, item_id=item.id, kind="summarize", state="queued", stage="queued",
              title=title, payload=payload, progress={})
    s.add(job)
    s.flush()
    return job


def cancel(s: DbSession, job: Job) -> None:
    if job.state == "queued":
        job.state, job.finished_at, job.error = "cancelled", utcnow(), None
    elif job.state == "running":
        job.cancel_requested = True
    job.updated_at = utcnow()


def retry(s: DbSession, job: Job) -> None:
    if job.state in ("failed", "cancelled"):
        job.state, job.stage, job.error, job.attempts = "queued", "queued", None, 0
        job.run_after, job.cancel_requested, job.finished_at, job.runner_id = utcnow(), False, None, None
        job.updated_at = utcnow()


# ---- claiming (system session, limited to the runner's user) -------------------------------

def fits(job: Job, caps: dict[str, Any]) -> bool:
    if job.kind not in (caps.get("kinds") or []):
        return False
    if job.kind == "transcribe":
        options = job.payload.get("options") or {}
        mode = options.get("mode")
        if mode not in (caps.get("transcribe_modes") or []):
            return False
        if mode == "local" and options.get("model") not in (caps.get("whisper_models") or []):
            return False
        url = (job.payload.get("source") or {}).get("url") or ""
        return sources.domain_allowed(url, caps.get("allowed_domains"))
    if job.kind in ("summarize", "translate"):
        return job.payload.get("role") in (caps.get("llm_roles") or [])
    return False


def requeue_expired(s: DbSession, user_id: int, settings: Settings) -> None:
    now = utcnow()
    expired = s.scalars(select(Job).where(Job.user_id == user_id, Job.state == "running",
                                          Job.lease_until < now).with_for_update(skip_locked=True)).all()
    for job in expired:
        if job.cancel_requested:
            job.state, job.finished_at = "cancelled", now
        elif job.attempts >= settings.job_max_attempts:
            job.state, job.finished_at = "failed", now
            job.error = "A futtató nem jelentkezett a munkával; a próbálkozások elfogytak."
        else:
            job.state, job.stage, job.runner_id, job.lease_until = "queued", "queued", None, None
            job.error = "A futtató nem jelentkezett; a munka visszakerült a sorba."
        job.updated_at = now


def claim(s: DbSession, runner: Runner, caps: dict[str, Any], settings: Settings) -> Job | None:
    requeue_expired(s, runner.user_id, settings)
    now = utcnow()
    candidates = s.scalars(select(Job).where(
        Job.user_id == runner.user_id, Job.state == "queued", Job.run_after <= now,
        Job.cancel_requested.is_(False), Job.kind.in_(caps.get("kinds") or []))
        .order_by(Job.id).limit(50).with_for_update(skip_locked=True)).all()
    job = next((j for j in candidates if fits(j, caps)), None)
    if job is None:
        return None
    job.state, job.stage, job.runner_id = "running", "starting", runner.id
    job.attempts += 1
    job.started_at = job.started_at or now
    job.lease_until = now + timedelta(seconds=settings.lease_s)
    job.updated_at = now
    return job


def job_input(s: DbSession, job: Job) -> dict[str, Any]:
    """What the runner gets for a claimed job."""
    if job.kind == "transcribe":
        return {"source": job.payload["source"], "options": job.payload["options"]}
    t = s.get(Transcript, job.payload["transcript_id"])
    if t is None:
        raise ProtocolError(410, "transcript gone")
    src = s.get(Source, t.source_id)
    meta = src.meta or {}
    if job.kind == "translate":
        return {"role": job.payload["role"], "source_language": t.language,
                "target": job.payload.get("target", TRANSLATION_TARGET),
                "meta": {"title": meta.get("title"), "channel": meta.get("channel"), "url": src.url},
                "segments": [{"idx": g.idx, "text": g.text} for g in t.segments]}
    return {
        "role": job.payload["role"], "language": job.payload.get("language", "hu"),
        "meta": {"title": meta.get("title"), "channel": meta.get("channel"),
                 "duration_s": meta.get("duration_s"), "url": src.url},
        "transcript": {"language": t.language, "mode": t.mode, "model": t.model, "caption_kind": t.caption_kind,
                       "diarized": t.diarized, "speakers": [sp.get("label") for sp in (t.speakers or [])],
                       "segments": [{"start": g.start_s, "end": g.end_s, "speaker": g.speaker, "text": g.text}
                                    for g in t.segments]},
    }


def running_job(s: DbSession, runner: Runner, job_id: int) -> Job:
    job = s.get(Job, job_id, with_for_update=True)
    if job is None or job.user_id != runner.user_id:
        raise ProtocolError(404, "no such job")
    if job.state != "running" or job.runner_id != runner.id:
        raise ProtocolError(409, "the job is not running on this runner")
    return job


def heartbeat(s: DbSession, runner: Runner, job_id: int, stage: str | None, progress: dict | None,
              settings: Settings) -> dict[str, Any]:
    job = running_job(s, runner, job_id)
    now = utcnow()
    job.lease_until = now + timedelta(seconds=settings.lease_s)
    if stage:
        job.stage = str(stage)[:40]
    if isinstance(progress, dict):
        job.progress = progress
    job.updated_at = now
    return {"lease_s": settings.lease_s, "cancel": job.cancel_requested}


def fail(s: DbSession, runner: Runner, job_id: int, error: str, retry_mode: str, settings: Settings) -> None:
    job = running_job(s, runner, job_id)
    now = utcnow()
    job.error, job.updated_at, job.runner_id, job.lease_until = (error or "")[:2000], now, None, None
    if job.cancel_requested:
        job.state, job.finished_at, job.error = "cancelled", now, None
    elif retry_mode == "later" and job.attempts < settings.job_max_attempts:
        delay = RETRY_MINUTES[min(job.attempts - 1, len(RETRY_MINUTES) - 1)]
        job.state, job.stage, job.run_after = "queued", "queued", now + timedelta(minutes=delay)
    else:
        job.state, job.finished_at = "failed", now


# ---- results ------------------------------------------------------------------------------

def _meta_update(src: Source, video: dict[str, Any]) -> None:
    meta = dict(src.meta or {})
    for key in ("title", "channel", "duration_s", "published", "thumbnail"):
        if video.get(key) not in (None, ""):
            meta[key] = video[key]
    src.meta, src.updated_at = meta, utcnow()


def store_transcription(s: DbSession, runner: Runner, job: Job, result: dict[str, Any]) -> Transcript:
    item = s.get(Item, job.item_id)
    if item is None:
        raise ProtocolError(410, "the item was deleted")
    src = s.get(Source, item.source_id)
    _meta_update(src, result.get("video") or {})
    tr = result["transcript"]
    segments = tr["segments"]
    options = job.payload.get("options") or {}
    t = Transcript(source_id=src.id, recipe_hash=recipe_hash({"source": src.key, "options": options}),
                   mode=tr["mode"], model=tr.get("model"), caption_kind=tr.get("caption_kind"),
                   language=tr.get("language"), diarized=bool(tr.get("diarized")),
                   speakers=tr.get("speakers") or [], warnings=tr.get("warnings") or [], cloud=tr.get("cloud"),
                   options=options, private=not item.is_public, shareable=item.is_public,
                   origin_user_id=job.user_id, runner_version=runner.version, elapsed_s=tr.get("elapsed_s"),
                   segment_count=len(segments))
    s.add(t)
    s.flush()
    s.add_all(Segment(transcript_id=t.id, idx=i, start_s=float(g["start"]), end_s=float(g["end"]),
                      speaker=g.get("speaker"), text=str(g["text"])) for i, g in enumerate(segments))
    s.add(ItemTranscript(item_id=item.id, transcript_id=t.id, user_id=job.user_id))
    if item.main_transcript_id is None or job.payload.get("make_main"):
        item.main_transcript_id = t.id
        item.current_translation_id = None  # the old main's translation and summary do not fit any more
        if job.payload.get("make_main"):
            item.current_summary_id = None
    item.updated_at = utcnow()
    _finish(job, {"transcript_id": t.id})
    if item.main_transcript_id == t.id:
        after_main_transcript(s, job.user_id, item, t, bool(job.payload.get("summarize")))
    return t


def after_main_transcript(s: DbSession, user_id: int, item: Item, t: Transcript, summarize: bool) -> None:
    """The follow-up steps of a new main transcript: summary (if asked), translation (if not
    Hungarian and the user wants it automatically), search index."""
    src = s.get(Source, item.source_id)
    title = (src.meta or {}).get("title") or src.url
    if summarize:
        enqueue_summary(s, user_id, item, t.id, title)
    user = s.get(User, user_id)
    if needs_translation(t.language) and (user is None or user.translate_auto):
        enqueue_translation(s, user_id, item, t.id, title)
    indexing.enqueue(s, item)


def store_summary(s: DbSession, runner: Runner, job: Job, result: dict[str, Any]) -> Summary:
    item = s.get(Item, job.item_id)
    if item is None:
        raise ProtocolError(410, "the item was deleted")
    tid = job.payload["transcript_id"]
    summary = Summary(
        transcript_id=tid, role=job.payload["role"],
        recipe_hash=recipe_hash({"transcript": tid, "role": job.payload["role"], "model": result.get("model"),
                                 "prompt_version": result.get("prompt_version"), "language": result.get("language")}),
        model=result.get("model"), model_used=result.get("model_used"), language=result.get("language") or "hu",
        prompt_version=int(result.get("prompt_version") or 0), text=result["text"],
        chunked=bool(result.get("chunked")), input_chars=int(result.get("input_chars") or 0),
        prompt_tokens=result.get("prompt_tokens"), completion_tokens=result.get("completion_tokens"),
        reasoning_tokens=result.get("reasoning_tokens"), duration_s=result.get("duration_s"),
        private=not item.is_public, shareable=item.is_public, origin_user_id=job.user_id,
        runner_version=runner.version)
    s.add(summary)
    s.flush()
    s.add(ItemSummary(item_id=item.id, summary_id=summary.id, user_id=job.user_id))
    if item.main_transcript_id == tid:
        item.current_summary_id = summary.id
        indexing.enqueue(s, item)
    item.updated_at = utcnow()
    _finish(job, {"summary_id": summary.id})
    return summary


def store_translation(s: DbSession, runner: Runner, job: Job, result: dict[str, Any]) -> Translation:
    item = s.get(Item, job.item_id)
    if item is None:
        raise ProtocolError(410, "the item was deleted")
    tid = job.payload["transcript_id"]
    t = s.get(Transcript, tid)
    if t is None:
        raise ProtocolError(410, "transcript gone")
    known = {g.idx for g in t.segments}
    lines = [g for g in result["segments"] if g["idx"] in known]
    tr = Translation(
        transcript_id=tid, role=job.payload["role"], language=result.get("language") or TRANSLATION_TARGET,
        recipe_hash=translation_recipe(tid, int(result.get("prompt_version") or 0),
                                       result.get("language") or TRANSLATION_TARGET),
        source_language=t.language, model=result.get("model"), model_used=result.get("model_used"),
        prompt_version=int(result.get("prompt_version") or 0), segment_count=len(known),
        missing=len(known) - len(lines), prompt_tokens=result.get("prompt_tokens"),
        completion_tokens=result.get("completion_tokens"), reasoning_tokens=result.get("reasoning_tokens"),
        duration_s=result.get("duration_s"), private=not item.is_public, shareable=item.is_public,
        origin_user_id=job.user_id, runner_version=runner.version)
    s.add(tr)
    s.flush()
    s.add_all(TranslationSegment(translation_id=tr.id, idx=int(g["idx"]), text=str(g["text"])) for g in lines)
    s.add(ItemTranslation(item_id=item.id, translation_id=tr.id, user_id=job.user_id))
    if item.main_transcript_id == tid:
        item.current_translation_id = tr.id
        indexing.enqueue(s, item)
    item.updated_at = utcnow()
    _finish(job, {"translation_id": tr.id})
    return tr


def translation_recipe(transcript_id: int, prompt_version: int, language: str) -> str:
    """Reusable for anyone with the same transcript: the model is the runner's business."""
    return recipe_hash({"transcript": transcript_id, "prompt_version": prompt_version, "language": language,
                        "kind": "translation"})


def _finish(job: Job, ref: dict[str, Any]) -> None:
    now = utcnow()
    job.state, job.stage, job.finished_at, job.updated_at = "done", "done", now, now
    job.lease_until, job.error = None, None
    job.progress = {**(job.progress or {}), "result": ref}


def store_result(s: DbSession, runner: Runner, job_id: int, result: dict[str, Any]) -> dict[str, Any]:
    """Idempotent: a repeated delivery of a finished job's result is acknowledged, not stored twice."""
    job = s.get(Job, job_id, with_for_update=True)
    if job is None or job.user_id != runner.user_id:
        raise ProtocolError(404, "no such job")
    if job.state == "done" and (job.progress or {}).get("result"):
        return job.progress["result"]
    if job.state != "running" or job.runner_id != runner.id:
        raise ProtocolError(409, "the job is not running on this runner")
    if job.kind == "transcribe":
        store_transcription(s, runner, job, result)
    elif job.kind == "translate":
        store_translation(s, runner, job, result)
    else:
        store_summary(s, runner, job, result)
    return job.progress["result"]


def touch_runner(s: DbSession, runner: Runner) -> None:
    s.execute(update(Runner).where(Runner.id == runner.id).values(last_seen_at=utcnow()))
