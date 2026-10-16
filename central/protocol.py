"""The runner protocol: /runner/v1/... (dwyt-docs/docs/protokoll.md).

Runners always call in; the central service never calls a runner. Every request carries the
`Dwyt-Protocol` header; authenticated requests carry `Authorization: Bearer <runner token>`.
"""
from __future__ import annotations

import asyncio
import secrets
import time
from datetime import timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from pydantic import BaseModel, Field, StringConstraints, ValidationError, field_validator
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as sa_insert
from starlette.concurrency import run_in_threadpool

from . import db, jobs, modelinfo, ownfiles, ratelimit, remote
from .config import get_settings
from .db import utcnow
from .models import Item, Job, PairRequest, Runner, Source, User
from .security import new_token, token_hash

PROTOCOL = 1
SUPPORTED = {PROTOCOL}  # the current and the previous version once there is one
PAIR_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
MAX_PENDING_PAIRINGS = 1000
Short = Annotated[str, StringConstraints(max_length=253)]

router = APIRouter(prefix="/runner/v1")


class ProtocolMismatch(Exception):
    pass


def check_version(dwyt_protocol: str | None = Header(default=None)) -> int:
    try:
        version = int(dwyt_protocol or "")
    except ValueError:
        raise ProtocolMismatch() from None
    if version not in SUPPORTED:
        raise ProtocolMismatch()
    return version


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


def _authenticate(token: str) -> Runner | None:
    with db.system_session() as s:
        runner = s.scalar(select(Runner).where(Runner.token_hash == token_hash(token)))
        if runner is None or runner.revoked_at is not None:
            return None
        user = s.get(User, runner.user_id)
        if user is None or user.disabled:
            return None
        if runner.last_seen_at is None or (utcnow() - runner.last_seen_at).total_seconds() > 10:
            runner.last_seen_at = utcnow()
            s.flush()  # before expunge: a detached object's change is never written (heartbeats kept it "offline")
        s.expunge(runner)
        return runner


async def current_runner(request: Request, _v: int = Depends(check_version)) -> Runner:
    auth = request.headers.get("authorization", "")
    token = auth[7:].strip() if auth[:7].lower() == "bearer " else ""
    runner = await run_in_threadpool(_authenticate, token) if token and len(token) < 200 else None
    if runner is None:
        raise RunnerUnauthorized()
    return runner


class RunnerUnauthorized(Exception):
    pass


# ---- pairing ------------------------------------------------------------------------------

def _pair_code() -> str:
    raw = "".join(secrets.choice(PAIR_ALPHABET) for _ in range(6))
    return f"{raw[:3]}-{raw[3:]}"


def normalise_code(code: str) -> str:
    raw = "".join(ch for ch in (code or "").upper() if ch in PAIR_ALPHABET)
    return f"{raw[:3]}-{raw[3:6]}" if len(raw) == 6 else ""


@router.post("/pair/start")
async def pair_start(request: Request, _v: int = Depends(check_version)):
    settings = get_settings()
    wait = ratelimit.hit("pair-start", request.client.host if request.client else None)
    if wait:
        return JSONResponse({"error": f"Túl sok párosítás erről a címről; próbáld újra {wait} másodperc múlva."},
                            status_code=429, headers={"Retry-After": str(wait)})

    def create() -> dict | None:
        poll = new_token()
        with db.system_session() as s:
            s.query(PairRequest).filter(PairRequest.expires_at < utcnow()).delete()
            if s.query(PairRequest).count() >= MAX_PENDING_PAIRINGS:
                return None
            for _ in range(5):
                code = _pair_code()
                if s.scalar(select(PairRequest.id).where(PairRequest.code == code)) is None:
                    break
            s.add(PairRequest(code=code, poll_hash=token_hash(poll),
                              expires_at=utcnow() + timedelta(minutes=settings.pair_minutes)))
        return {"code": code, "poll_id": poll, "expires_in": settings.pair_minutes * 60}

    created = await run_in_threadpool(create)
    if created is None:
        return _error(429, "Túl sok függő párosítás; próbáld később.")
    return created


class PairPoll(BaseModel):
    poll_id: str = Field(min_length=10, max_length=200)


@router.post("/pair/poll")
async def pair_poll(body: PairPoll, _v: int = Depends(check_version)):
    def poll() -> tuple[int, dict]:
        with db.system_session() as s:
            req = s.scalar(select(PairRequest).where(PairRequest.poll_hash == token_hash(body.poll_id))
                           .with_for_update())
            if req is None or req.expires_at <= utcnow():
                return 410, {"error": "A párosítókód lejárt; indítsd újra a párosítást."}
            if req.approved_at is None or req.user_id is None:
                return 202, {"status": "pending"}
            token = new_token()
            runner = Runner(user_id=req.user_id, name=req.runner_name or "futtató", token_hash=token_hash(token),
                            capabilities={})
            s.add(runner)
            s.delete(req)
            s.flush()
            return 200, {"runner_id": runner.id, "name": runner.name, "token": token}

    status, data = await run_in_threadpool(poll)
    return JSONResponse(data, status_code=status)


class PairRedeem(BaseModel):
    code: str = Field(min_length=4, max_length=20)
    name: str = Field(default="", max_length=200)


@router.post("/pair/redeem")
async def pair_redeem(body: PairRedeem, request: Request, format: str = "", _v: int = Depends(check_version)):
    """The one-line installer trades its 5-minute install code for a runner token (protocol: "Telepítés
    egy paranccsal"). ?format=env answers in lines a plain sh can read."""
    wait = ratelimit.hit("install-redeem", request.client.host if request.client else None)
    if wait:
        return JSONResponse({"error": f"Túl sok próbálkozás erről a címről; próbáld újra {wait} másodperc múlva."},
                            status_code=429, headers={"Retry-After": str(wait)})
    done = await run_in_threadpool(remote.redeem, body.code, body.name)
    if done is None:
        message = ("A telepítőkód lejárt, már felhasználták, vagy nincs ilyen. Kérj új parancsot a dwyt "
                   "Futtatók oldalán.")
        if format == "env":
            return PlainTextResponse(f"ERROR={message}\n", status_code=410)
        return _error(410, message)
    if format == "env":
        return PlainTextResponse(f"DWYT_RUNNER_ID={done['runner_id']}\nDWYT_TOKEN={done['token']}\n",
                                 headers={"Cache-Control": "no-store"})
    return JSONResponse(done, headers={"Cache-Control": "no-store"})


def pairing_collected(user_id: int, code: str) -> bool:
    """False while this user's approved code still waits for its runner to fetch the token."""
    code = normalise_code(code)
    if not code:
        return True
    with db.system_session() as s:
        req = s.scalar(select(PairRequest).where(PairRequest.code == code))
        return req is None or req.user_id != user_id or req.expires_at <= utcnow()


def approve_pairing(user_id: int, code: str, name: str) -> bool:
    """Called from the web UI by the signed-in user who typed the code."""
    code = normalise_code(code)
    if not code:
        return False
    with db.system_session() as s:
        req = s.scalar(select(PairRequest).where(PairRequest.code == code).with_for_update())
        if req is None or req.expires_at <= utcnow() or req.approved_at is not None:
            return False
        req.user_id, req.runner_name, req.approved_at = user_id, (name.strip() or "futtató")[:80], utcnow()
        return True


# ---- jobs ---------------------------------------------------------------------------------

class Capabilities(BaseModel):
    # plain strings: a newer runner may announce kinds/modes this central does not know yet;
    # they simply never match a job
    kinds: list[Short] = Field(default_factory=list, max_length=20)
    transcribe_modes: list[Short] = Field(default_factory=list, max_length=20)
    llm_roles: list[Short] = Field(default_factory=list, max_length=20)
    allowed_domains: list[Short] = Field(default_factory=list, max_length=100)
    max_upload_bytes: int | None = None
    local_sources: bool = False
    whisper_models: list[Annotated[str, StringConstraints(max_length=40)]] = Field(default_factory=list, max_length=20)
    # where the media may come from besides URLs: upload (staged here), local (the runner's watched folder)
    source_kinds: list[Short] = Field(default_factory=list, max_length=20)
    # models the user may choose per role (public / private) for summaries and translations
    llm_models: dict[Short, Annotated[list[Annotated[str, StringConstraints(max_length=100)]],
                                      Field(max_length=30)]] = Field(default_factory=dict, max_length=5)
    # what runs behind each model name (round 6; from dwyt-airouter's LiteLLM), checked again by modelinfo
    llm_model_info: dict[Annotated[str, StringConstraints(max_length=100)], dict[str, Any]] = Field(
        default_factory=dict, max_length=40)
    llm_server: str = Field(default="", max_length=60)
    llm_defaults: dict[Annotated[str, StringConstraints(max_length=20)],
                       Annotated[str, StringConstraints(max_length=100)]] = Field(default_factory=dict, max_length=10)

    @field_validator("llm_model_info")
    @classmethod
    def _small_info(cls, value):
        import json
        if len(json.dumps(value)) > 40_000:
            raise ValueError("llm_model_info too large")
        return value


class ClaimBody(BaseModel):
    capabilities: Capabilities
    runner_version: str | None = Field(default=None, max_length=40)
    tool_versions: dict[Annotated[str, StringConstraints(max_length=40)],
                        Annotated[str, StringConstraints(max_length=40)]] = Field(default_factory=dict, max_length=20)
    # the jobs the runner still works on while asking (None: an older runner that does not say). A job this
    # runner holds on our side but not on its own was lost (the runner restarted): it goes back to the queue.
    holding: list[int] | None = Field(default=None, max_length=50)


def _claim_once(runner: Runner, body: ClaimBody, version: int) -> dict | None:
    settings = get_settings()
    caps = body.capabilities.model_dump()
    for key in ("llm_model_info", "llm_server", "llm_defaults"):
        if key not in body.capabilities.model_fields_set:  # an older runner: the page says so
            caps.pop(key, None)
    if "llm_model_info" in caps:
        caps["llm_model_info"] = modelinfo.sanitize(caps["llm_model_info"])
    with db.system_session() as s:
        r = s.get(Runner, runner.id)
        r.capabilities = {**caps, "tool_versions": body.tool_versions}
        r.version, r.protocol, r.last_seen_at = body.runner_version, version, utcnow()
        if body.holding is not None:
            jobs.release_lost(s, r, body.holding, settings)
        job = jobs.claim(s, r, caps, settings)
        if job is None:
            return None
        return {"job_id": job.id, "kind": job.kind, "lease_s": settings.lease_s, "input": jobs.job_input(s, job)}


@router.post("/claim")
async def claim(body: ClaimBody, runner: Runner = Depends(current_runner), version: int = Depends(check_version)):
    """Long-poll for a job. `Dwyt-Settings: pending` says settings changes wait (then no long waiting)."""
    settings = get_settings()
    deadline = time.monotonic() + settings.claim_wait_s
    while True:
        found = await run_in_threadpool(_claim_once, runner, body, version)
        pending = await run_in_threadpool(remote.has_pending, runner.id)
        headers = {"Dwyt-Settings": "pending"} if pending else {}
        if found is not None:
            return JSONResponse(found, headers=headers)
        if pending or time.monotonic() + settings.claim_poll_s > deadline:
            return Response(status_code=204, headers=headers)
        await asyncio.sleep(settings.claim_poll_s)


# ---- remote settings (round 7d) ------------------------------------------------------------------

class SettingsReport(BaseModel):
    runner_version: str | None = Field(default=None, max_length=40)
    latest_version: str | None = Field(default=None, max_length=40)
    install: Literal["service", "app"] | None = None
    remote: bool = False
    public_key: str | None = Field(default=None, max_length=2000)
    describe: dict[str, Any] | None = None
    modes: list[dict[str, Any]] | None = Field(default=None, max_length=10)
    mode: str | None = Field(default=None, max_length=40)
    values: dict[Annotated[str, StringConstraints(max_length=60)],
                 Annotated[str, StringConstraints(max_length=4000)]] | None = Field(default=None, max_length=200)
    secrets_set: list[Annotated[str, StringConstraints(max_length=60)]] | None = Field(default=None, max_length=20)

    @field_validator("describe", "modes")
    @classmethod
    def _small(cls, value):
        import json
        if value is not None and len(json.dumps(value)) > 200_000:
            raise ValueError("too large")
        return value

    @field_validator("public_key")
    @classmethod
    def _pem(cls, value):
        if value is not None and not value.startswith("-----BEGIN PUBLIC KEY-----"):
            raise ValueError("public_key must be a PEM public key")
        return value


@router.put("/settings")
async def settings_report(body: SettingsReport, runner: Runner = Depends(current_runner)):
    pending = await run_in_threadpool(remote.store_report, runner.id, body.model_dump())
    return {"pending": pending}


@router.get("/settings/changes")
async def settings_changes(runner: Runner = Depends(current_runner)):
    return {"changes": await run_in_threadpool(remote.take, runner.id)}


class ChangeResult(BaseModel):
    ok: bool = True
    applied: list[Annotated[str, StringConstraints(max_length=60)]] = Field(default_factory=list, max_length=200)
    errors: dict[Annotated[str, StringConstraints(max_length=60)],
                 Annotated[str, StringConstraints(max_length=500)]] = Field(default_factory=dict, max_length=200)
    checks: dict[Annotated[str, StringConstraints(max_length=60)], list[Any]] = Field(default_factory=dict,
                                                                                      max_length=20)
    message: str = Field(default="", max_length=1000)


@router.post("/settings/changes/{change_id}/result")
async def settings_change_result(change_id: int, body: ChangeResult, runner: Runner = Depends(current_runner)):
    ok = await run_in_threadpool(remote.finish, runner.id, change_id, body.model_dump())
    return {"ok": True} if ok else _error(404, "no such open change")


class HeartbeatBody(BaseModel):
    stage: str | None = Field(default=None, max_length=40)
    progress: dict[str, Any] | None = None

    @field_validator("progress")
    @classmethod
    def _small(cls, value):
        import json
        if value is not None and len(json.dumps(value)) > 4000:
            raise ValueError("progress too large")
        return value


def _protocol_call(fn, *args):
    try:
        with db.system_session() as s:
            return 200, fn(s, *args)
    except jobs.ProtocolError as exc:
        return exc.status, {"error": str(exc)}


@router.post("/jobs/{job_id}/heartbeat")
async def heartbeat(job_id: int, body: HeartbeatBody, runner: Runner = Depends(current_runner)):
    status, data = await run_in_threadpool(_protocol_call, jobs.heartbeat, runner, job_id, body.stage,
                                           body.progress, get_settings())
    return JSONResponse(data, status_code=status)


class FailBody(BaseModel):
    error: str = Field(max_length=4000)
    retry: Literal["later", "never", "next_step"] = "never"


@router.post("/jobs/{job_id}/fail")
async def fail(job_id: int, body: FailBody, runner: Runner = Depends(current_runner)):
    # next_step continues a subscription video's chain; elsewhere it is final, like never
    status, data = await run_in_threadpool(
        _protocol_call, lambda s, *a: jobs.fail(s, *a) or {"ok": True}, runner, job_id, body.error, body.retry,
        get_settings())
    return JSONResponse(data, status_code=status)


# result shapes

class SegmentIn(BaseModel):
    start: float = Field(ge=0)
    end: float = Field(ge=0)
    speaker: str | None = Field(default=None, max_length=100)
    text: str = Field(max_length=20000)


class SpeakerIn(BaseModel):
    label: str = Field(max_length=100)
    share: float | None = None


class TranscriptIn(BaseModel):
    mode: Literal["captions", "cloud", "local"]
    model: str | None = Field(default=None, max_length=100)
    caption_kind: Literal["manual", "auto"] | None = None
    language: str | None = Field(default=None, max_length=20)
    diarized: bool = False
    speakers: list[SpeakerIn] = Field(default_factory=list, max_length=100)
    warnings: list[str] = Field(default_factory=list, max_length=20)
    cloud: dict[str, Any] | None = None
    elapsed_s: float | None = None
    segments: list[SegmentIn] = Field(min_length=1, max_length=200000)


class VideoIn(BaseModel):
    title: str | None = Field(default=None, max_length=500)
    channel: str | None = Field(default=None, max_length=300)
    duration_s: int | None = Field(default=None, ge=0)
    published: str | None = Field(default=None, max_length=20)


class PageMediaIn(BaseModel):
    kind: Literal["youtube", "player", "dayfile", "podcast", "file"]
    url: str = Field(max_length=2000, pattern=r"^https?://")
    label: str = Field(max_length=200)
    video_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{11}$")


class PageIn(BaseModel):
    """The page a recording was found on (the runner's page resolver, decision 19.10)."""
    url: str = Field(max_length=2000)
    title: str | None = Field(default=None, max_length=500)
    site: str | None = Field(default=None, max_length=200)
    published: str | None = Field(default=None, max_length=40)
    text: str | None = Field(default=None, max_length=8000)
    chosen: PageMediaIn
    candidates: list[PageMediaIn] = Field(default_factory=list, max_length=10)


class TranscribeResult(BaseModel):
    video: VideoIn = Field(default_factory=VideoIn)
    transcript: TranscriptIn
    page: PageIn | None = None


class SummaryResult(BaseModel):
    text: str = Field(min_length=1, max_length=200000)
    model: str | None = Field(default=None, max_length=200)
    model_used: str | None = Field(default=None, max_length=200)
    language: str = Field(default="hu", max_length=20)
    prompt_version: int = 0
    chunked: bool = False
    input_chars: int = 0
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    reasoning_tokens: int | None = None
    duration_s: float | None = None


# ---- own recordings ---------------------------------------------------------------------------

def _media_path(runner: Runner, job_id: int) -> tuple[int, str]:
    with db.system_session() as s:
        job = s.get(Job, job_id)
        if job is None or not jobs.may_see(runner, job):
            return 404, "no such job"
        if job.state != "running" or job.runner_id != runner.id:
            return 409, "the job is not running on this runner"
        src = job.payload.get("source") or {}
        if src.get("kind") != "upload":
            return 404, "this job has no uploaded file"
        path = ownfiles.staged_path(get_settings(), src.get("sha256", ""))
        if not path.is_file():
            return 410, "the uploaded file is gone (expired); upload it again"
        return 200, str(path)


@router.get("/jobs/{job_id}/media")
async def media(job_id: int, runner: Runner = Depends(current_runner)):
    """The uploaded file of a running job, for the runner that holds it (HTTP Range supported)."""
    status, value = await run_in_threadpool(_media_path, runner, job_id)
    if status != 200:
        return _error(status, value)
    return FileResponse(value, media_type="application/octet-stream")


class LocalItem(BaseModel):
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    name: str = Field(min_length=1, max_length=500)
    size: int = Field(ge=1)
    options: dict[str, Any] = Field(default_factory=dict)
    summarize: bool = True


def _register_local(runner: Runner, body: LocalItem) -> tuple[int, dict]:
    from .web import parse_options  # one place for the option rules
    form = {k: str(v) for k, v in body.options.items() if v is not None}
    form.setdefault("mode", "local")
    if "model" in form:
        form["model_" + form["mode"]] = form.pop("model")
    options, errors = parse_options(form)
    if options.get("mode") == "captions":
        errors.append("Saját felvételhez YouTube-felirat nem kérhető.")
    if errors:
        return 422, {"error": " ".join(errors)}
    settings = get_settings()
    try:
        ownfiles.check_storage(settings, runner.user_id)
    except ownfiles.QuotaError as exc:
        return 403, {"error": str(exc)}
    key = f"upload:{body.sha256}"
    with db.system_session() as s:
        s.execute(sa_insert(Source).values(kind="upload", key=key, url=None, meta={})
                  .on_conflict_do_nothing(index_elements=[Source.key]))
        source_id = s.scalar(select(Source.id).where(Source.key == key))
    name = ownfiles.clean_name(body.name)
    with db.user_session(runner.user_id) as s:
        item = s.scalar(select(Item).where(Item.source_id == source_id))
        if item is not None and item.main_transcript_id is not None:
            return 200, {"item_id": item.id, "job_id": None, "existing": True}
        if item is None:
            item = Item(user_id=runner.user_id, source_id=source_id, is_public=False, title=name)
            s.add(item)
            s.flush()
        active = s.scalar(select(Job).where(Job.item_id == item.id, Job.kind == "transcribe",
                                            Job.state.in_(("queued", "running"))))
        if active is not None:
            return 200, {"item_id": item.id, "job_id": active.id, "existing": False}
        job = jobs.enqueue_transcription(
            s, runner.user_id, item, s.get(Source, source_id), options, body.summarize,
            file={"kind": "local", "sha256": body.sha256, "name": name, "size": body.size, "runner_id": runner.id})
        from . import indexing
        indexing.enqueue(s, item)
        return 200, {"item_id": item.id, "job_id": job.id, "existing": False}


@router.post("/local-items")
async def local_items(body: LocalItem, runner: Runner = Depends(current_runner)):
    """A new file in the runner's watched folder: create the item and a transcription job that only
    this runner can claim (the file never leaves its machine)."""
    status, data = await run_in_threadpool(_register_local, runner, body)
    return JSONResponse(data, status_code=status)


class TranslatedLine(BaseModel):
    idx: int = Field(ge=0)
    text: str = Field(max_length=20000)


class TranslateResult(BaseModel):
    language: str = Field(default="hu", max_length=20)
    model: str | None = Field(default=None, max_length=200)
    model_used: str | None = Field(default=None, max_length=200)
    prompt_version: int = 0
    segments: list[TranslatedLine] = Field(min_length=1, max_length=200000)
    missing: int = 0
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    reasoning_tokens: int | None = None
    duration_s: float | None = None


class AskResult(BaseModel):
    """The answer to a question (kind ask): Markdown in Hungarian."""
    text: str = Field(min_length=1, max_length=100000)
    model: str | None = Field(default=None, max_length=200)
    model_used: str | None = Field(default=None, max_length=200)
    prompt_version: int = 0
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    reasoning_tokens: int | None = None
    duration_s: float | None = None


RESULT_MODELS = {"transcribe": TranscribeResult, "summarize": SummaryResult, "translate": TranslateResult,
                 "ask": AskResult}


def _job_kind(runner: Runner, job_id: int) -> str | None:
    with db.system_session() as s:
        job = s.get(Job, job_id)
        return job.kind if job is not None and jobs.may_see(runner, job) else None


@router.put("/jobs/{job_id}/result")
async def result(job_id: int, request: Request, runner: Runner = Depends(current_runner)):
    kind = await run_in_threadpool(_job_kind, runner, job_id)
    if kind is None:
        return _error(404, "no such job")
    try:
        raw = await request.json()
        model = RESULT_MODELS[kind].model_validate(raw)
    except (ValueError, ValidationError) as exc:
        return _error(422, f"invalid result: {str(exc)[:1000]}")
    status, data = await run_in_threadpool(_protocol_call, jobs.store_result, runner, job_id,
                                           model.model_dump())
    return JSONResponse(data, status_code=status)
