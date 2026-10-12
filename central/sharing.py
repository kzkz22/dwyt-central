"""Runner sharing (decision 17.5): a runner's owner offers it to another dwyt user.

The owner decides what the runner may do for that user (YouTube captions, local Whisper, local
language models, the user's own recordings) and gives monthly cloud limits: hours of cloud
transcription and answers of a cloud language model (0 = none). The other user accepts the offer
first, because their videos and texts pass through the owner's machine.

What a shared runner may do for a user is its "effective capabilities": the runner's own
capabilities cut down to the share. Claiming (jobs.claim), the pages and the model pickers all
use them, so the limits hold everywhere in the same way. Without cloud language-model budget the
user's language jobs go out with the private role, i.e. the runner's local-only model (the one
own recordings use), and wait for the GPU host instead of falling back to the cloud.

A user's own runner comes first: a shared runner leaves a job alone while one of the user's own
runners is online and can do it. Between users a shared runner takes jobs in turn.

Usage is counted per calendar month (the instance's time zone) after a result arrives, so the
last job of a month may overshoot the limit a little; the next one waits for the next month.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session as DbSession

from . import db
from .config import get_settings
from .db import utcnow
from .models import Job, Runner, RunnerShare, ShareUsage, User

ONLINE_S = 90
LLM_KINDS = ("summarize", "translate", "ask")
MAX_SHARES_PER_RUNNER = 20


class ShareError(Exception):
    """Shown to the user as it is (Hungarian)."""


def month_key(now: datetime | None = None) -> str:
    return (now or utcnow()).astimezone(ZoneInfo(get_settings().timezone)).strftime("%Y-%m")


def online(runner: Any) -> bool:
    seen = getattr(runner, "last_seen_at", None)
    return bool(seen) and (utcnow() - seen).total_seconds() < ONLINE_S


# ---- what a model name does ------------------------------------------------------------------

def _last(name: str | None) -> str:
    return (name or "").strip().lower().rsplit("/", 1)[-1]


def local_only(info: dict[str, Any] | None) -> bool:
    return bool(info) and info.get("where") == "local" and not info.get("fallback")


def ran_in_cloud(info: dict[str, Any] | None, model_used: str | None) -> bool:
    """Did this answer come from the cloud? A cloud name always; a local name only when the model
    that answered is one of its cloud fallbacks (LiteLLM reports the fallback's model)."""
    if not info:
        return False
    if info.get("where") == "cloud":
        return True
    used = _last(model_used)
    if not used or used == _last(info.get("target")):
        return False
    return any(used in (_last(f.get("target")), _last(f.get("name"))) for f in info.get("fallback") or []
               if f.get("target") or f.get("name"))


# ---- effective capabilities -------------------------------------------------------------------

@dataclass
class Remaining:
    cloud_seconds: float
    llm_answers: int


def remaining(share: RunnerShare, usage: ShareUsage | None) -> Remaining:
    used_s = usage.cloud_seconds if usage else 0.0
    used_a = usage.llm_answers if usage else 0
    return Remaining(max(0.0, share.cloud_hours * 3600 - used_s), max(0, share.llm_answers - used_a))


def effective_caps(caps: dict[str, Any], share: RunnerShare, left: Remaining) -> dict[str, Any]:
    """The runner's capabilities as far as this share allows them now."""
    caps = caps or {}
    modes = [m for m in caps.get("transcribe_modes") or []
             if (m == "captions" and share.allow_captions) or (m == "local" and share.allow_local)
             or (m == "cloud" and left.cloud_seconds > 0)]
    roles = caps.get("llm_roles") or []
    offered = caps.get("llm_models") or {}
    private_only = left.llm_answers <= 0
    out_roles: list[str] = []
    models: dict[str, list[str]] = {}
    if share.allow_llm:
        if private_only:  # public texts too, but only with the runner's local-only model
            if "private" in roles:
                out_roles.append("public")
                models["public"] = list(offered.get("private") or [])
        elif "public" in roles:
            out_roles.append("public")
            models["public"] = list(offered.get("public") or [])
        if share.allow_own and "private" in roles:
            out_roles.append("private")
            models["private"] = list(offered.get("private") or [])
    kinds = [k for k in caps.get("kinds") or []
             if (k == "transcribe" and modes) or (k in LLM_KINDS and out_roles)]
    names = {n for names in models.values() for n in names}
    info = {n: v for n, v in (caps.get("llm_model_info") or {}).items() if n in names}
    defaults = dict(caps.get("llm_defaults") or {})
    if private_only and defaults.get("private"):
        defaults.update(summarize=defaults["private"], translate=defaults["private"], ask=defaults["private"])
    out = {**caps, "kinds": kinds, "transcribe_modes": modes, "llm_roles": out_roles, "llm_models": models,
           "source_kinds": ["upload"] if share.allow_own and "upload" in (caps.get("source_kinds") or []) else [],
           "shared_private_llm": private_only}
    if "llm_model_info" in caps:
        out["llm_model_info"] = info
    if "llm_defaults" in caps:
        out["llm_defaults"] = {k: v for k, v in defaults.items() if k != "private" or share.allow_own}
    return out


# ---- what the pages see -----------------------------------------------------------------------

@dataclass
class SharedRunner:
    """A runner someone shared with the viewer, as the pages and pickers use it (no token hash)."""
    id: int
    name: str
    user_id: int  # the owner
    owner_name: str
    last_seen_at: datetime | None
    version: str | None
    protocol: int | None
    capabilities: dict[str, Any]
    share: dict[str, Any]
    revoked_at: None = None
    shared: bool = True
    usage: dict[str, Any] = field(default_factory=dict)


def _share_dict(sh: RunnerShare, usage: ShareUsage | None) -> dict[str, Any]:
    return {"id": sh.id, "allow_captions": sh.allow_captions, "allow_local": sh.allow_local,
            "allow_llm": sh.allow_llm, "allow_own": sh.allow_own, "cloud_hours": sh.cloud_hours,
            "llm_answers": sh.llm_answers, "accepted": sh.accepted_at is not None,
            "used_hours": round((usage.cloud_seconds if usage else 0) / 3600, 2),
            "used_answers": usage.llm_answers if usage else 0, "used_tokens": usage.llm_tokens if usage else 0}


def _display(user: User | None) -> str:
    return (user.name or user.email) if user is not None else "?"


def shared_with(user_id: int, accepted: bool = True) -> list[SharedRunner]:
    """Runners shared with this user (accepted ones, or the open offers)."""
    out: list[SharedRunner] = []
    month = month_key()
    with db.system_session() as s:
        rows = s.execute(select(RunnerShare, Runner).join(Runner, Runner.id == RunnerShare.runner_id)
                         .where(RunnerShare.user_id == user_id, Runner.revoked_at.is_(None),
                                RunnerShare.accepted_at.is_not(None) if accepted
                                else RunnerShare.accepted_at.is_(None))
                         .order_by(RunnerShare.id)).all()
        for sh, r in rows:
            owner = s.get(User, sh.owner_id)
            if owner is None or owner.disabled:
                continue
            usage = s.get(ShareUsage, (sh.id, month))
            out.append(SharedRunner(
                id=r.id, name=r.name, user_id=r.user_id, owner_name=_display(owner), last_seen_at=r.last_seen_at,
                version=r.version, protocol=r.protocol,
                capabilities=effective_caps(r.capabilities or {}, sh, remaining(sh, usage)),
                share=_share_dict(sh, usage)))
    return out


def usable_runners(s: DbSession, user_id: int) -> list[Any]:
    """The user's own runners, then the runners shared with them (accepted)."""
    own = list(s.scalars(select(Runner).where(Runner.revoked_at.is_(None)).order_by(Runner.id)))
    return own + shared_with(user_id)


def owner_shares(owner_id: int) -> dict[int, list[dict[str, Any]]]:
    """runner id -> the shares of that runner, for its owner's page."""
    out: dict[int, list[dict[str, Any]]] = {}
    month = month_key()
    with db.system_session() as s:
        for sh in s.scalars(select(RunnerShare).where(RunnerShare.owner_id == owner_id).order_by(RunnerShare.id)):
            user = s.get(User, sh.user_id)
            row = _share_dict(sh, s.get(ShareUsage, (sh.id, month)))
            row.update(name=_display(user), email=user.email if user is not None else "")
            out.setdefault(sh.runner_id, []).append(row)
    return out


def open_offers(user_id: int) -> int:
    with db.system_session() as s:
        return s.scalar(select(func.count()).select_from(RunnerShare).join(Runner, Runner.id == RunnerShare.runner_id)
                        .where(RunnerShare.user_id == user_id, RunnerShare.accepted_at.is_(None),
                               Runner.revoked_at.is_(None))) or 0


# ---- changes (system session, explicit checks) -----------------------------------------------

def _limits(cloud_hours: Any, llm_answers: Any) -> tuple[float, int]:
    try:
        hours = round(float(str(cloud_hours or 0).replace(",", ".")), 2)
        answers = int(str(llm_answers or 0))
    except ValueError:
        raise ShareError("A keret csak szám lehet.") from None
    if not 0 <= hours <= 1000 or not 0 <= answers <= 100_000:
        raise ShareError("A keret 0 és 1000 óra, illetve 0 és 100 000 válasz között lehet.")
    return hours, answers


def offer(owner_id: int, runner_id: int, email: str, options: dict[str, Any]) -> None:
    """Share a runner with an existing dwyt user (or change an existing share's settings)."""
    email = (email or "").strip().lower()
    hours, answers = _limits(options.get("cloud_hours"), options.get("llm_answers"))
    flags = {k: bool(options.get(k)) for k in ("allow_captions", "allow_local", "allow_llm", "allow_own")}
    if not (flags["allow_captions"] or flags["allow_local"] or flags["allow_llm"] or hours):
        raise ShareError("Válassz legalább egy dolgot, amit használhat.")
    with db.system_session() as s:
        runner = s.get(Runner, runner_id)
        if runner is None or runner.user_id != owner_id or runner.revoked_at is not None:
            raise ShareError("Nincs ilyen futtatód.")
        user = s.scalar(select(User).where(func.lower(User.email) == email)) if email else None
        if user is None or user.disabled:
            raise ShareError("Nincs ilyen dwyt-felhasználó. Csak olyannal oszthatod meg, akinek már van fiókja.")
        if user.id == owner_id:
            raise ShareError("Magaddal nem oszthatod meg.")
        sh = s.scalar(select(RunnerShare).where(RunnerShare.runner_id == runner_id, RunnerShare.user_id == user.id))
        if sh is None:
            count = s.scalar(select(func.count()).select_from(RunnerShare)
                             .where(RunnerShare.runner_id == runner_id)) or 0
            if count >= MAX_SHARES_PER_RUNNER:
                raise ShareError(f"Egy futtatót legfeljebb {MAX_SHARES_PER_RUNNER} felhasználóval oszthatsz meg.")
            sh = RunnerShare(runner_id=runner_id, owner_id=owner_id, user_id=user.id)
            s.add(sh)
        for key, value in flags.items():
            setattr(sh, key, value)
        sh.cloud_hours, sh.llm_answers, sh.updated_at = hours, answers, utcnow()


def update(owner_id: int, share_id: int, options: dict[str, Any]) -> None:
    with db.system_session() as s:
        sh = s.get(RunnerShare, share_id)
        if sh is None or sh.owner_id != owner_id:
            raise ShareError("Nincs ilyen megosztás.")
        user = s.get(User, sh.user_id)
        runner_id = sh.runner_id
    offer(owner_id, runner_id, user.email if user is not None else "", options)


def accept(user_id: int, share_id: int) -> None:
    with db.system_session() as s:
        sh = s.get(RunnerShare, share_id)
        if sh is None or sh.user_id != user_id:
            raise ShareError("Nincs ilyen felajánlás.")
        if sh.accepted_at is None:
            sh.accepted_at = sh.updated_at = utcnow()


def remove(user_id: int, share_id: int) -> None:
    """The owner ends a share, or the user declines / leaves it. Jobs already running finish."""
    with db.system_session() as s:
        sh = s.get(RunnerShare, share_id)
        if sh is None or user_id not in (sh.owner_id, sh.user_id):
            raise ShareError("Nincs ilyen megosztás.")
        s.delete(sh)


# ---- the runner side (system session) --------------------------------------------------------

def claim_caps(s: DbSession, runner: Runner, caps: dict[str, Any]) -> dict[int, dict[str, Any]]:
    """user id -> effective capabilities, for the users this runner is shared with (accepted)."""
    month = month_key()
    out: dict[int, dict[str, Any]] = {}
    for sh in s.scalars(select(RunnerShare).where(RunnerShare.runner_id == runner.id,
                                                  RunnerShare.accepted_at.is_not(None))):
        user = s.get(User, sh.user_id)
        if user is None or user.disabled:
            continue
        out[sh.user_id] = effective_caps(caps, sh, remaining(sh, s.get(ShareUsage, (sh.id, month))))
    return out


def own_runner_can(s: DbSession, user_id: int, job: Job, fits) -> bool:
    """Is one of the user's own runners online and able to do this job? Then it is theirs."""
    for r in s.scalars(select(Runner).where(Runner.user_id == user_id, Runner.revoked_at.is_(None))):
        if online(r) and fits(job, r.capabilities or {}, r.id):
            return True
    return False


def turn_order(s: DbSession, runner: Runner, users: list[int]) -> list[int]:
    """The users in turn: the one this runner served longest ago first."""
    last = dict(s.execute(select(Job.user_id, func.max(Job.started_at))
                          .where(Job.runner_id == runner.id, Job.user_id.in_(users)).group_by(Job.user_id)).all())
    floor = datetime.min.replace(tzinfo=utcnow().tzinfo)
    return sorted(users, key=lambda u: (last.get(u) or floor, u != runner.user_id, u))


def record_usage(s: DbSession, runner: Runner, job: Job, result: dict[str, Any]) -> None:
    """Count what a shared user's finished job used of the owner's cloud budget."""
    if job.user_id == runner.user_id:
        return
    sh = s.scalar(select(RunnerShare).where(RunnerShare.runner_id == runner.id, RunnerShare.user_id == job.user_id))
    if sh is None:
        return
    seconds, answers, tokens = 0.0, 0, 0
    if job.kind == "transcribe":
        tr = result.get("transcript") or {}
        if tr.get("mode") == "cloud":
            cloud = tr.get("cloud") or {}
            seconds = float(cloud.get("audio_seconds") or (result.get("video") or {}).get("duration_s") or 0)
    elif job.kind in LLM_KINDS:
        info = ((runner.capabilities or {}).get("llm_model_info") or {}).get(result.get("model") or "")
        if ran_in_cloud(info, result.get("model_used")):
            answers = 1
            tokens = int(result.get("prompt_tokens") or 0) + int(result.get("completion_tokens") or 0)
    if not (seconds or answers):
        return
    month = month_key()
    usage = s.get(ShareUsage, (sh.id, month), with_for_update=True)
    if usage is None:
        usage = ShareUsage(share_id=sh.id, month=month, cloud_seconds=0, llm_answers=0, llm_tokens=0)
        s.add(usage)
    usage.cloud_seconds += seconds
    usage.llm_answers += answers
    usage.llm_tokens += tokens
