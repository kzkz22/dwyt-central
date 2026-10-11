"""Own recordings and quotas (plan 6, 8, 17; decisions 13.4, 13.5).

Browser uploads are written to STAGING_DIR/<sha256> (one file per content, shared by users who
upload the same bytes) and kept only until a runner has delivered the transcript, or at most
UPLOAD_KEEP_DAYS. Files in a runner's watched folder never come here: the runner registers them
(local-items) and processes them itself.

Own recordings are private: is_public is false, results are never reused by others, summaries
and translations use the runner's private model.

Quotas: upload size and the number of uploads waiting for a runner; the text and vectors of the
user's library (each user is charged for every result linked to his items, reused ones too).
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session as DbSession

from . import db
from .config import Settings
from .db import utcnow
from .models import Upload, User

log = logging.getLogger("dwyt.files")
SHA = re.compile(r"^[0-9a-f]{64}$")
VECTOR_BYTES = 4 * 1024 + 8
FREE_MARGIN = 512 * 1024 ** 2


class QuotaError(Exception):
    """A user-facing reason (Hungarian)."""


def clean_name(name: str) -> str:
    """A displayable file name: no path, no control characters, at most 200 characters."""
    name = unicodedata.normalize("NFC", (name or "").replace("\\", "/").split("/")[-1])
    name = "".join(ch for ch in name if ch.isprintable()).strip() or "feltoltes"
    return name[:200]


def staged_path(settings: Settings, sha: str) -> Path:
    if not SHA.match(sha or ""):
        raise ValueError("bad sha256")
    return Path(settings.staging_dir) / sha


@dataclass
class Limits:
    storage_bytes: int
    upload_bytes: int
    pending: int


def limits(settings: Settings, user: User | None) -> Limits:
    storage = (user.quota_storage_mb if user and user.quota_storage_mb is not None else settings.storage_quota_mb)
    upload = (user.quota_upload_mb if user and user.quota_upload_mb is not None else settings.upload_max_mb)
    return Limits(storage * 1024 ** 2, upload * 1024 ** 2, settings.upload_pending_max)


def user_limits(settings: Settings, user_id: int) -> Limits:
    with db.system_session() as s:
        return limits(settings, s.get(User, user_id))


_USAGE = text("""
    SELECT
      (SELECT coalesce(sum(length(g.text)), 0) FROM segments g
         JOIN item_transcripts it ON it.transcript_id = g.transcript_id WHERE it.user_id = :u)
    + (SELECT coalesce(sum(length(g.text)), 0) FROM translation_segments g
         JOIN item_translations it ON it.translation_id = g.translation_id WHERE it.user_id = :u)
    + (SELECT coalesce(sum(length(sm.text)), 0) FROM summaries sm
         JOIN item_summaries it ON it.summary_id = sm.id WHERE it.user_id = :u)
    + (SELECT coalesce(sum(length(c.text) + coalesce(length(c.text_hu), 0)
                           + CASE WHEN c.embedding IS NULL THEN 0 ELSE :vb END), 0)
         FROM chunks c JOIN items i ON i.source_id = c.source_id AND i.user_id = :u AND (
              (c.kind = 'meta' AND (c.item_id IS NULL OR c.item_id = i.id))
           OR (c.kind = 'transcript' AND c.transcript_id = i.main_transcript_id
               AND c.translation_id IS NOT DISTINCT FROM i.current_translation_id)
           OR (c.kind = 'summary' AND c.summary_id = i.current_summary_id)))
""")


def storage_used(user_id: int) -> int:
    """Approximate bytes of text and vectors in the user's library (length() counts characters)."""
    with db.system_session() as s:
        return int(s.scalar(_USAGE, {"u": user_id, "vb": VECTOR_BYTES}) or 0)


def pending_uploads(s: DbSession, user_id: int) -> int:
    return s.scalar(select(func.count()).select_from(Upload).where(Upload.user_id == user_id,
                                                                    Upload.consumed_at.is_(None))) or 0


def check_storage(settings: Settings, user_id: int) -> None:
    lim = user_limits(settings, user_id)
    used = storage_used(user_id)
    if used >= lim.storage_bytes:
        raise QuotaError(f"Elfogyott a tárhelyed ({used / 1024 ** 2:.0f} MB / {lim.storage_bytes / 1024 ** 2:.0f} MB "
                         "szöveg és vektor). Törölj régi elemeket, vagy kérj nagyobb keretet az admintól.")


async def receive(request, settings: Settings, user_id: int, name: str) -> Upload:
    """Stream the request body into staging; one file per content (sha256)."""
    lim = user_limits(settings, user_id)
    with db.user_session(user_id) as s:
        if pending_uploads(s, user_id) >= lim.pending:
            raise QuotaError(f"Egyszerre legfeljebb {lim.pending} feltöltés várhat feldolgozásra; várd meg, "
                             "amíg a futtatód átveszi őket.")
    check_storage(settings, user_id)
    staging = Path(settings.staging_dir)
    staging.mkdir(parents=True, exist_ok=True)
    tmp = staging / f"incoming-{uuid.uuid4().hex}.part"
    digest, size = hashlib.sha256(), 0
    free = shutil.disk_usage(staging).free
    try:
        with open(tmp, "wb") as fh:
            async for chunk in request.stream():
                size += len(chunk)
                if size > lim.upload_bytes:
                    raise QuotaError(f"A fájl nagyobb a megengedettnél ({lim.upload_bytes / 1024 ** 2:.0f} MB).")
                if size > free - FREE_MARGIN:
                    raise QuotaError("A központon most nincs elég szabad hely a feltöltéshez; próbáld később.")
                digest.update(chunk)
                fh.write(chunk)
        if size == 0:
            raise QuotaError("Üres fájl.")
        sha = digest.hexdigest()
        target = staged_path(settings, sha)
        if target.exists():
            tmp.unlink()
        else:
            os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)
    with db.user_session(user_id) as s:
        up = Upload(user_id=user_id, sha256=sha, name=clean_name(name), size=size)
        s.add(up)
        s.flush()
        s.expunge(up)
        return up


def consume(s: DbSession, settings: Settings, upload_id: int) -> None:
    """The transcript of this upload was delivered: mark it done, delete the file when no other
    upload (of anyone, same content) waits for it. System session."""
    sha = s.scalar(text("UPDATE uploads SET consumed_at = now() WHERE id = :id AND consumed_at IS NULL "
                        "RETURNING sha256"), {"id": upload_id})
    if sha:
        _delete_if_unused(s, settings, sha)


def _delete_if_unused(s: DbSession, settings: Settings, sha: str) -> None:
    waiting = s.scalar(text("SELECT count(*) FROM uploads WHERE sha256 = :sha AND consumed_at IS NULL"), {"sha": sha})
    if not waiting:
        try:
            staged_path(settings, sha).unlink(missing_ok=True)
        except OSError as exc:
            log.warning("could not delete staged %s: %s", sha, exc)


def housekeeping(settings: Settings) -> None:
    """Expire old uploads and delete files nobody waits for; remove stray partial files."""
    cutoff = utcnow() - timedelta(days=settings.upload_keep_days)
    with db.system_session() as s:
        expired = s.execute(text("UPDATE uploads SET consumed_at = now() WHERE consumed_at IS NULL "
                                 "AND created_at < :cut RETURNING sha256"), {"cut": cutoff}).scalars().all()
        for sha in set(expired):
            _delete_if_unused(s, settings, sha)
        staging = Path(settings.staging_dir)
        if staging.is_dir():
            waiting = set(s.scalars(text("SELECT DISTINCT sha256 FROM uploads WHERE consumed_at IS NULL")).all())
            hour_ago = utcnow().timestamp() - 3600  # a file being registered right now is not an orphan
            for path in staging.iterdir():
                mtime = path.stat().st_mtime
                stale_part = path.name.endswith(".part") and mtime < cutoff.timestamp()
                orphan = SHA.match(path.name) and path.name not in waiting and mtime < hour_ago
                if stale_part or orphan:
                    path.unlink(missing_ok=True)
