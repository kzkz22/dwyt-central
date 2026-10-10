"""The web application:  uvicorn central.web:app

Users sign in with Google (accounts.py); admins may also use password + TOTP. Every page of a
signed-in user reads and writes through user_session(), so row-level security limits it to
that user's rows. State-changing requests carry the session's CSRF token (hidden form field
or the X-CSRF-Token header).
"""
from __future__ import annotations

import logging
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from starlette.concurrency import run_in_threadpool

from . import accounts, db, jobs, library, protocol, security, sources
from .config import Settings, get_settings
from .db import utcnow
from .models import (ACTIVE, Invite, Item, ItemTranscript, Job, Runner, Session, Source, Summary,
                     Transcript, User)

log = logging.getLogger("dwyt")
HERE = Path(__file__).resolve().parent
PAGE_SIZE = 30
ONLINE_S = 90

app = FastAPI(title="dwyt", docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
app.include_router(protocol.router)
templates = Jinja2Templates(directory=HERE / "templates")


def _local(value, fmt: str = "%Y-%m-%d %H:%M") -> str:
    return value.astimezone(ZoneInfo(get_settings().timezone)).strftime(fmt) if value else ""


templates.env.filters.update(local=_local)
templates.env.globals.update(hms=library.hms, stage_label=jobs.stage_label, state_labels=jobs.STATE_LABELS,
                             describe_transcript=library.describe_transcript, mode_labels=library.MODE_LABELS)

CSP = ("default-src 'self'; img-src 'self' https://i.ytimg.com; style-src 'self'; script-src 'self'; "
       "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self' https://accounts.google.com")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("Content-Security-Policy", CSP)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault("X-Frame-Options", "DENY")
    if not request.url.path.startswith("/static/"):
        response.headers.setdefault("Cache-Control", "no-store")
    return response


@app.exception_handler(protocol.ProtocolMismatch)
async def _protocol_mismatch(_request: Request, _exc):
    return JSONResponse({"error": "Ez a futtató túl régi vagy túl új ehhez a központhoz; frissítsd.",
                         "supported": sorted(protocol.SUPPORTED)}, status_code=426)


@app.exception_handler(protocol.RunnerUnauthorized)
async def _runner_unauthorized(_request: Request, _exc):
    return JSONResponse({"error": "A futtató tokenje érvénytelen vagy visszavonták; párosítsd újra."},
                        status_code=401, headers={"WWW-Authenticate": "Bearer"})


# ---- who is asking ------------------------------------------------------------------------

class LoginRequired(Exception):
    pass


class BadCsrf(Exception):
    pass


class Forbidden(Exception):
    pass


@app.exception_handler(LoginRequired)
async def _login_required(request: Request, _exc):
    if request.headers.get("HX-Request"):
        return Response(status_code=200, headers={"HX-Redirect": "/belepes"})
    return RedirectResponse("/belepes", status_code=303)


@app.exception_handler(BadCsrf)
async def _bad_csrf(_request: Request, _exc):
    return PlainTextResponse("Lejárt vagy hibás űrlap. Töltsd újra az oldalt.", status_code=403)


@app.exception_handler(Forbidden)
async def _forbidden(_request: Request, _exc):
    return PlainTextResponse("Ehhez nincs jogosultságod.", status_code=403)


@dataclass
class Viewer:
    id: int
    email: str
    name: str | None
    role: str
    csrf: str

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


def _viewer(token: str | None) -> Viewer | None:
    with db.system_session() as s:
        sess = security.find_session(s, token)
        if sess is None or sess.state != "active":
            return None
        user = s.get(User, sess.user_id)
        if user is None or user.disabled:
            return None
        if (utcnow() - sess.last_seen_at).total_seconds() > 60:
            sess.last_seen_at = utcnow()
        return Viewer(user.id, user.email, user.name, user.role, sess.csrf_token)


async def current_viewer(request: Request) -> Viewer:
    viewer = await run_in_threadpool(_viewer, request.cookies.get(security.COOKIE))
    if viewer is None:
        raise LoginRequired()
    return viewer


async def authed_post(request: Request) -> Viewer:
    viewer = await current_viewer(request)
    sent = request.headers.get("X-CSRF-Token")
    if sent is None:
        sent = str((await request.form()).get("csrf_token", ""))
    if not secrets.compare_digest(sent, viewer.csrf):
        raise BadCsrf()
    return viewer


async def admin_viewer(request: Request) -> Viewer:
    viewer = await current_viewer(request)
    if not viewer.is_admin:
        raise Forbidden()
    return viewer


async def admin_post(request: Request) -> Viewer:
    viewer = await authed_post(request)
    if not viewer.is_admin:
        raise Forbidden()
    return viewer


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def _set_cookie(response: Response, token: str, settings: Settings, max_age: int | None) -> None:
    response.set_cookie(security.COOKIE, token, max_age=max_age, httponly=True, samesite="lax",
                        secure=settings.cookie_secure, path="/")


def _render(request: Request, name: str, viewer: Viewer | None = None, status_code: int = 200, **ctx: Any):
    return templates.TemplateResponse(request, name, {"viewer": viewer, "csrf": viewer.csrf if viewer else "",
                                                      **ctx}, status_code=status_code)


def _redirect(url: str) -> Response:
    return RedirectResponse(url, status_code=303)


def _message(request: Request, viewer: Viewer | None, title: str, message: str, back: str = "/",
             status_code: int = 200):
    return _render(request, "message.html", viewer, status_code=status_code, title=title, message=message,
                   back=back)


# ---- sign-in ------------------------------------------------------------------------------

@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/belepes", response_class=HTMLResponse)
def login_page(request: Request, hiba: str = ""):
    settings = get_settings()
    return _render(request, "login.html", error=hiba[:300], google=bool(settings.google_client_id))


@app.get("/meghivo/{token}", response_class=HTMLResponse)
def invite_page(request: Request, token: str):
    with db.system_session() as s:
        invite = accounts.invite_by_token(s, token)
        email = invite.email if invite else None
    if email is None:
        return _message(request, None, "A meghívó nem érvényes",
                        "A meghívó lejárt, visszavonták, vagy már felhasználták. Kérj újat.", "/belepes", 404)
    return _render(request, "invite.html", email=email, token=token)


LOGIN_COOKIE = "dwyt_signin"


@app.get("/auth/google/start")
def google_start(meghivo: str = ""):
    settings = get_settings()
    try:
        with db.system_session() as s:
            url, state = accounts.start(s, settings, meghivo[:100] or None)
    except accounts.SignInError as exc:
        return _redirect(f"/belepes?hiba={exc}")
    response = RedirectResponse(url, status_code=302)
    # binds the sign-in to this browser: a callback link made in another browser is refused
    response.set_cookie(LOGIN_COOKIE, state, max_age=accounts.STATE_MINUTES * 60, httponly=True, samesite="lax",
                        secure=settings.cookie_secure, path="/auth/google")
    return response


@app.get("/auth/google/callback")
async def google_callback(request: Request, state: str = "", code: str = "", error: str = ""):
    settings = get_settings()
    if error or not code:
        return _message(request, None, "A belépés nem sikerült", "A Google-belépést megszakítottad vagy "
                        "a Google elutasította. Próbáld újra.", "/belepes", 400)
    browser_state = request.cookies.get(LOGIN_COOKIE, "")

    def sign_in() -> str:
        with db.system_session() as s:  # consumed even when the rest fails
            login = accounts.consume_state(s, state)
        if not browser_state or not secrets.compare_digest(browser_state, state):
            raise accounts.SignInError("A belépést ebben a böngészőben kell elkezdeni. Kezdd újra.")
        who, invite_hash = accounts.finish(login, settings, code)
        with db.system_session() as s:
            user = accounts.resolve_user(s, settings, who, invite_hash)
            security.record_success(user)
            security.purge_expired(s)
            return security.start_session(s, user, "active", settings.session_days, _client_ip(request),
                                          request.headers.get("user-agent"))

    try:
        token = await run_in_threadpool(sign_in)
    except accounts.SignInError as exc:
        response = _message(request, None, "A belépés nem sikerült", str(exc), "/belepes", 403)
        response.delete_cookie(LOGIN_COOKIE, path="/auth/google")
        return response
    response = _redirect("/")
    response.delete_cookie(LOGIN_COOKIE, path="/auth/google")
    _set_cookie(response, token, settings, int(settings.session_days * 86400))
    return response


@app.get("/admin-belepes", response_class=HTMLResponse)
def admin_login_page(request: Request):
    return _render(request, "admin_login.html", step="password")


@app.post("/admin-belepes")
async def admin_login(request: Request):
    form = await request.form()
    email, password = str(form.get("email", ""))[:200], str(form.get("password", ""))[:500]
    settings = get_settings()

    def attempt() -> tuple[str | None, str | None]:
        with db.system_session() as s:
            user = security.user_by_email(s, email)
            if user is not None and (user.role != "admin" or user.disabled or not user.password_hash):
                user = None
            if user is not None and security.lock_seconds_left(user):
                return None, f"Túl sok hibás próbálkozás; várj {security.lock_seconds_left(user)} másodpercet."
            if not security.check_password(user, password):
                if user is not None:
                    security.record_failure(user)
                return None, "Hibás e-mail-cím vagy jelszó."
            return security.start_session(s, user, "pending", settings.session_days, _client_ip(request),
                                          request.headers.get("user-agent")), None

    token, error = await run_in_threadpool(attempt)
    if error:
        return _render(request, "admin_login.html", step="password", error=error, email=email, status_code=401)
    response = _redirect("/admin-belepes/kod")
    _set_cookie(response, token, settings, security.PENDING_MINUTES * 60)
    return response


def _pending(request: Request) -> Session | None:
    with db.system_session() as s:
        sess = security.find_session(s, request.cookies.get(security.COOKIE))
        if sess is None or sess.state != "pending":
            return None
        s.expunge(sess)
        return sess


@app.get("/admin-belepes/kod", response_class=HTMLResponse)
def admin_code_page(request: Request):
    sess = _pending(request)
    if sess is None:
        return _redirect("/admin-belepes")
    return _render(request, "admin_login.html", step="code", csrf=sess.csrf_token)


@app.post("/admin-belepes/kod")
async def admin_code(request: Request):
    form = await request.form()
    settings = get_settings()
    old_token = request.cookies.get(security.COOKIE)

    def attempt() -> tuple[str | None, str | None]:
        with db.system_session() as s:
            sess = security.find_session(s, old_token)
            if sess is None or sess.state != "pending":
                return None, "expired"
            if not secrets.compare_digest(str(form.get("csrf_token", "")), sess.csrf_token):
                return None, "expired"
            user = s.get(User, sess.user_id, with_for_update=True)
            if security.lock_seconds_left(user):
                return None, f"Túl sok hibás próbálkozás; várj {security.lock_seconds_left(user)} másodpercet."
            if not security.check_totp(user, str(form.get("code", ""))):
                security.record_failure(user)
                return None, "Hibás kód."
            security.record_success(user)
            security.end_session(s, old_token)
            return security.start_session(s, user, "active", settings.session_days, _client_ip(request),
                                          request.headers.get("user-agent")), None

    token, error = await run_in_threadpool(attempt)
    if error == "expired":
        return _redirect("/admin-belepes")
    if error:
        sess = _pending(request)
        return _render(request, "admin_login.html", step="code", error=error,
                       csrf=sess.csrf_token if sess else "", status_code=401)
    response = _redirect("/")
    _set_cookie(response, token, settings, int(settings.session_days * 86400))
    return response


@app.post("/kilepes")
async def logout(request: Request, viewer: Viewer = Depends(authed_post)):
    def end() -> None:
        with db.system_session() as s:
            security.end_session(s, request.cookies.get(security.COOKIE))
    await run_in_threadpool(end)
    response = _redirect("/belepes")
    response.delete_cookie(security.COOKIE, path="/")
    return response


# ---- library ------------------------------------------------------------------------------

def _title(item: Item) -> str:
    meta = item.source.meta or {}
    return item.title or meta.get("title") or item.source.url or item.source.key


def _thumb(source: Source) -> str | None:
    if source.kind == "youtube":
        return f"https://i.ytimg.com/vi/{source.key.split(':', 1)[1]}/mqdefault.jpg"
    return None


templates.env.globals.update(item_title=_title, thumb=_thumb)


@app.get("/", response_class=HTMLResponse)
def library_page(request: Request, oldal: int = 1, viewer: Viewer = Depends(current_viewer)):
    page = max(1, oldal)
    with db.user_session(viewer.id) as s:
        total = s.scalar(select(func.count()).select_from(Item)) or 0
        items = s.scalars(select(Item).order_by(Item.created_at.desc(), Item.id.desc())
                          .offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE)).all()
        active = set(s.scalars(select(Job.item_id).where(Job.state.in_(ACTIVE))).all())
        has_runner = s.scalar(select(func.count()).select_from(Runner).where(Runner.revoked_at.is_(None))) or 0
    return _render(request, "library.html", viewer, items=items, active=active, page=page,
                   pages=max(1, -(-total // PAGE_SIZE)), total=total, has_runner=has_runner)


@app.get("/elem/{item_id}", response_class=HTMLResponse)
def item_page(request: Request, item_id: int, atirat: int | None = None, viewer: Viewer = Depends(current_viewer)):
    with db.user_session(viewer.id) as s:
        item = s.get(Item, item_id)
        if item is None:
            return _message(request, viewer, "Nincs ilyen elem", "Ez az elem nem létezik.", "/", 404)
        transcripts = s.scalars(select(Transcript).join(ItemTranscript, ItemTranscript.transcript_id == Transcript.id)
                                .where(ItemTranscript.item_id == item.id).order_by(Transcript.id)).all()
        chosen = next((t for t in transcripts if t.id == (atirat or item.main_transcript_id)), None)
        if chosen is None and transcripts:
            chosen = transcripts[-1]
        summary = s.get(Summary, item.current_summary_id) if item.current_summary_id else None
        item_jobs = s.scalars(select(Job).where(Job.item_id == item.id).order_by(Job.id.desc()).limit(10)).all()
        paragraphs = library.turns(chosen.segments) if chosen else []
        links = {p["start"]: library.timestamp_url(item.source, p["start"]) for p in paragraphs}
    return _render(request, "item.html", viewer, item=item, transcripts=transcripts, transcript=chosen,
                   paragraphs=paragraphs, links=links, summary=summary, item_jobs=item_jobs)


@app.post("/elem/{item_id}/torles")
async def delete_item(item_id: int, viewer: Viewer = Depends(authed_post)):
    def remove() -> None:
        with db.user_session(viewer.id) as s:
            item = s.get(Item, item_id)
            if item is not None:
                s.delete(item)
    await run_in_threadpool(remove)
    return _redirect("/")


@app.post("/elem/{item_id}/osszefoglalo")
async def resummarize(request: Request, item_id: int, viewer: Viewer = Depends(authed_post)):
    def enqueue() -> str | None:
        with db.user_session(viewer.id) as s:
            item = s.get(Item, item_id)
            if item is None or item.main_transcript_id is None:
                return "Ehhez az elemhez még nincs átirat."
            busy = s.scalar(select(Job.id).where(Job.item_id == item.id, Job.kind == "summarize",
                                                 Job.state.in_(ACTIVE)))
            if busy:
                return "Az összefoglaló már készül."
            jobs.enqueue_summary(s, viewer.id, item, item.main_transcript_id, _title(item))
            return None
    error = await run_in_threadpool(enqueue)
    if error:
        return _message(request, viewer, "Az összefoglaló nem indult el", error, f"/elem/{item_id}", 422)
    return _redirect(f"/elem/{item_id}")


# ---- submit -------------------------------------------------------------------------------

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
        speakers = (form.get("num_speakers") or "").strip()
        if speakers:
            if not speakers.isdigit() or not 1 <= int(speakers) <= 20:
                errors.append("A beszélők száma 1 és 20 között legyen.")
            else:
                options["num_speakers"] = int(speakers)
    return options, errors


@app.get("/uj", response_class=HTMLResponse)
def submit_page(request: Request, viewer: Viewer = Depends(current_viewer)):
    return _render(request, "submit.html", viewer, form={"summarize": "on"}, errors=[], modes=MODES,
                   cloud_models=CLOUD_MODELS, local_models=LOCAL_MODELS)


def _source_for(ref: sources.SourceRef) -> int:
    with db.system_session() as s:
        s.execute(insert(Source).values(kind=ref.kind, key=ref.key, url=ref.url, meta={})
                  .on_conflict_do_nothing(index_elements=[Source.key]))
        return s.scalar(select(Source.id).where(Source.key == ref.key))


@app.post("/uj")
async def submit(request: Request, viewer: Viewer = Depends(authed_post)):
    form = {k: str(v) for k, v in (await request.form()).items()}
    options, errors = parse_options(form)
    ref = None
    try:
        ref = sources.parse(form.get("url", ""))
    except sources.BadUrl as exc:
        errors.append(str(exc))
    if ref is not None and options.get("mode") == "captions" and ref.kind != "youtube":
        errors.append("YouTube-feliratot csak YouTube-videóhoz lehet kérni.")
    if errors:
        return _render(request, "submit.html", viewer, form=form, errors=errors, modes=MODES,
                       cloud_models=CLOUD_MODELS, local_models=LOCAL_MODELS, status_code=422)
    source_id = await run_in_threadpool(_source_for, ref)

    def enqueue() -> int:
        with db.user_session(viewer.id) as s:
            item = s.scalar(select(Item).where(Item.source_id == source_id))
            if item is None:
                item = Item(user_id=viewer.id, source_id=source_id, is_public=True)
                s.add(item)
                s.flush()
            source = s.get(Source, source_id)
            jobs.enqueue_transcription(s, viewer.id, item, source, options, form.get("summarize") == "on")
            return item.id

    item_id = await run_in_threadpool(enqueue)
    return _redirect(f"/elem/{item_id}")


# ---- jobs ---------------------------------------------------------------------------------

def _jobs_context(viewer: Viewer) -> dict[str, Any]:
    with db.user_session(viewer.id) as s:
        active = s.scalars(select(Job).where(Job.state.in_(ACTIVE)).order_by(Job.id)).all()
        recent = s.scalars(select(Job).where(Job.state.not_in(ACTIVE)).order_by(Job.finished_at.desc().nulls_last(),
                                                                              Job.id.desc()).limit(30)).all()
        runners = s.scalars(select(Runner).where(Runner.revoked_at.is_(None))).all()
    online = [r for r in runners if r.last_seen_at and (utcnow() - r.last_seen_at).total_seconds() < ONLINE_S]
    # queued jobs that none of the runners has declared it can do (e.g. local Whisper without a GPU)
    unfit = {j.id for j in active if j.state == "queued" and runners
             and not any(jobs.fits(j, r.capabilities or {}) for r in runners)}
    return {"active": active, "recent": recent, "runner_count": len(runners), "online_count": len(online),
            "unfit": unfit}


@app.get("/munkak", response_class=HTMLResponse)
def jobs_page(request: Request, viewer: Viewer = Depends(current_viewer)):
    return _render(request, "jobs.html", viewer, **_jobs_context(viewer))


@app.get("/munkak/lista", response_class=HTMLResponse)
def jobs_partial(request: Request, viewer: Viewer = Depends(current_viewer)):
    return _render(request, "_jobs.html", viewer, **_jobs_context(viewer))


@app.post("/munkak/{job_id}/megszakitas")
async def cancel_job(job_id: int, viewer: Viewer = Depends(authed_post)):
    def run() -> None:
        with db.user_session(viewer.id) as s:
            job = s.get(Job, job_id, with_for_update=True)
            if job is not None:
                jobs.cancel(s, job)
    await run_in_threadpool(run)
    return _redirect("/munkak")


@app.post("/munkak/{job_id}/ujra")
async def retry_job(job_id: int, viewer: Viewer = Depends(authed_post)):
    def run() -> None:
        with db.user_session(viewer.id) as s:
            job = s.get(Job, job_id, with_for_update=True)
            if job is not None:
                jobs.retry(s, job)
    await run_in_threadpool(run)
    return _redirect("/munkak")


# ---- runners ------------------------------------------------------------------------------

_pair_failures: dict[int, list[float]] = {}


def _pair_rate_limited(user_id: int) -> bool:
    now = time.monotonic()
    recent = [t for t in _pair_failures.get(user_id, []) if now - t < 600]
    _pair_failures[user_id] = recent
    return len(recent) >= 10


def _runners_context(viewer: Viewer) -> dict[str, Any]:
    with db.user_session(viewer.id) as s:
        runners = s.scalars(select(Runner).order_by(Runner.revoked_at.is_not(None), Runner.id)).all()
    now = utcnow()
    return {"runners": runners,
            "online": {r.id for r in runners if r.last_seen_at and (now - r.last_seen_at).total_seconds() < ONLINE_S},
            "protocol": protocol.PROTOCOL, "base_url": get_settings().base_url}


@app.get("/futtatok", response_class=HTMLResponse)
def runners_page(request: Request, viewer: Viewer = Depends(current_viewer)):
    return _render(request, "runners.html", viewer, **_runners_context(viewer))


@app.post("/futtatok/parositas")
async def pair_runner(request: Request, viewer: Viewer = Depends(authed_post)):
    form = await request.form()
    if _pair_rate_limited(viewer.id):
        return _render(request, "runners.html", viewer, status_code=429, **_runners_context(viewer),
                       error="Túl sok hibás kód; várj tíz percet.")
    ok = await run_in_threadpool(protocol.approve_pairing, viewer.id, str(form.get("code", "")),
                                 str(form.get("name", "")))
    if not ok:
        _pair_failures.setdefault(viewer.id, []).append(time.monotonic())
        return _render(request, "runners.html", viewer, status_code=422, **_runners_context(viewer),
                       error="Nincs ilyen érvényes párosítókód. Nézd meg a futtató kiírását, és ügyelj rá, "
                             "hogy 10 percig érvényes.")
    return _render(request, "runners.html", viewer, **_runners_context(viewer),
                   notice="Jóváhagyva. A futtató pár másodpercen belül megkapja a tokenjét, és munkát kér.")


@app.post("/futtatok/{runner_id}/visszavonas")
async def revoke_runner(runner_id: int, viewer: Viewer = Depends(authed_post)):
    def run() -> None:
        with db.user_session(viewer.id) as s:
            runner = s.get(Runner, runner_id)
            if runner is not None and runner.revoked_at is None:
                runner.revoked_at = utcnow()
    await run_in_threadpool(run)
    return _redirect("/futtatok")


# ---- admin --------------------------------------------------------------------------------

def _admin_context() -> dict[str, Any]:
    with db.system_session() as s:
        users = s.scalars(select(User).order_by(User.id)).all()
        counts = dict(s.execute(select(Item.user_id, func.count()).group_by(Item.user_id)).all())
        runner_counts = dict(s.execute(select(Runner.user_id, func.count()).where(Runner.revoked_at.is_(None))
                                       .group_by(Runner.user_id)).all())
        invites = s.scalars(select(Invite).order_by(Invite.id.desc()).limit(50)).all()
    return {"users": users, "item_counts": counts, "runner_counts": runner_counts, "invites": invites,
            "now": utcnow(), "registration": get_settings().registration}


@app.get("/admin", response_class=HTMLResponse)
def admin_page(request: Request, viewer: Viewer = Depends(admin_viewer)):
    return _render(request, "admin.html", viewer, **_admin_context())


@app.post("/admin/meghivo")
async def create_invite(request: Request, viewer: Viewer = Depends(admin_post)):
    email = accounts.valid_email(str((await request.form()).get("email", "")))
    if email is None:
        return _render(request, "admin.html", viewer, status_code=422, **_admin_context(),
                       error="Adj meg egy érvényes e-mail-címet.")

    def create() -> str:
        with db.system_session() as s:
            return accounts.create_invite(s, get_settings(), email, viewer.id)
    link = await run_in_threadpool(create)
    return _render(request, "admin.html", viewer, **_admin_context(), invite_link=link, invite_email=email)


@app.post("/admin/meghivo/{invite_id}/visszavonas")
async def revoke_invite(invite_id: int, viewer: Viewer = Depends(admin_post)):
    def run() -> None:
        with db.system_session() as s:
            invite = s.get(Invite, invite_id)
            if invite is not None and invite.used_at is None:
                invite.revoked_at = utcnow()
    await run_in_threadpool(run)
    return _redirect("/admin")


@app.post("/admin/felhasznalo/{user_id}/{action}")
async def user_action(user_id: int, action: str, viewer: Viewer = Depends(admin_post)):
    if action not in ("tiltas", "engedelyezes") or user_id == viewer.id:
        return _redirect("/admin")

    def run() -> None:
        with db.system_session() as s:
            user = s.get(User, user_id)
            if user is None:
                return
            user.disabled = action == "tiltas"
            if user.disabled:
                security.end_all_sessions(s, user.id)
    await run_in_threadpool(run)
    return _redirect("/admin")


def purge() -> None:
    """Housekeeping: expired sessions."""
    with db.system_session() as s:
        security.purge_expired(s)
