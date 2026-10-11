"""The web application:  uvicorn central.web:app

Users sign in with Google (accounts.py); admins may also use password + TOTP. Every page of a
signed-in user reads and writes through user_session(), so row-level security limits it to
that user's rows. State-changing requests carry the session's CSRF token (hidden form field
or the X-CSRF-Token header).
"""
from __future__ import annotations

import hashlib
import logging
import secrets
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert
from starlette.concurrency import run_in_threadpool

from . import (accounts, db, indexing, items, jobs, library, ownfiles, protocol, ratelimit, render, reuse, search,
               secretbox, security, sources, subscriptions, youtube)
from .config import Settings, get_settings
from .db import utcnow
from .models import (ACTIVE, Channel, InboxEntry, Invite, Item, ItemSummary, ItemTranscript, ItemTranslation, Job,
                     LoginState, Runner, Session, Source, Subscription, Summary, Transcript, Translation, Upload, User)

log = logging.getLogger("dwyt")
HERE = Path(__file__).resolve().parent
PAGE_SIZE = 30
ONLINE_S = 90

@asynccontextmanager
async def _lifespan(_app: FastAPI):
    threads = []
    if get_settings().indexer:
        threads.append(indexing.Indexer())
    if get_settings().background:
        threads.append(subscriptions.Background())
    for thread in threads:
        thread.start()
    yield
    for thread in threads:
        thread.stop.set()


app = FastAPI(title="dwyt", docs_url=None, redoc_url=None, openapi_url=None, lifespan=_lifespan)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
app.include_router(protocol.router)
templates = Jinja2Templates(directory=HERE / "templates")


def _local(value, fmt: str = "%Y-%m-%d %H:%M") -> str:
    return value.astimezone(ZoneInfo(get_settings().timezone)).strftime(fmt) if value else ""


def _asset_versions() -> dict[str, str]:
    """Content hash per static file, so a changed file gets a new URL and no stale copy is used."""
    out = {}
    for path in (HERE / "static").rglob("*"):
        if path.is_file():
            out[path.relative_to(HERE / "static").as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()[:12]
    return out


ASSET_VERSIONS = _asset_versions()


def asset(name: str) -> str:
    return f"/static/{name}?v={ASSET_VERSIONS.get(name, '0')}"


templates.env.filters.update(local=_local)
templates.env.globals.update(asset=asset)
templates.env.globals.update(hms=library.hms, stage_label=jobs.stage_label, state_labels=jobs.STATE_LABELS,
                             describe_transcript=library.describe_transcript, mode_labels=library.MODE_LABELS,
                             kind_labels=jobs.KIND_LABELS)

CSP = ("default-src 'self'; img-src 'self' https://i.ytimg.com; style-src 'self'; script-src 'self'; "
       "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self' https://accounts.google.com")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("Content-Security-Policy", CSP)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault("X-Frame-Options", "DENY")
    if request.url.path.startswith("/static/"):
        # versioned URLs (asset()) may be kept; unversioned ones are checked again every time
        versioned = "v" in request.query_params
        response.headers.setdefault("Cache-Control", "public, max-age=31536000, immutable" if versioned
                                    else "no-cache")
    else:
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


def _limited(request: Request, name: str) -> Response | None:
    wait = ratelimit.hit(name, _client_ip(request))
    if not wait:
        return None
    log.warning("rate limit %s hit by %s", name, _client_ip(request))
    return _message(request, None, "Túl sok próbálkozás", f"Túl sok kérés érkezett erről a címről; "
                    f"próbáld újra {wait} másodperc múlva.", "/belepes", 429)


# Sign-in errors are shown by code, never as text from the URL (no injected messages on our page).
LOGIN_ERRORS = {"google-nincs": "A Google-belépés nincs beállítva (GOOGLE_CLIENT_ID)."}


# ---- sign-in ------------------------------------------------------------------------------

@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/belepes", response_class=HTMLResponse)
def login_page(request: Request, hiba: str = ""):
    settings = get_settings()
    return _render(request, "login.html", error=LOGIN_ERRORS.get(hiba, ""), google=bool(settings.google_client_id))


@app.get("/meghivo/{token}", response_class=HTMLResponse)
def invite_page(request: Request, token: str):
    if (limited := _limited(request, "invite")) is not None:
        return limited
    with db.system_session() as s:
        invite = accounts.invite_by_token(s, token)
        email = invite.email if invite else None
    if email is None:
        return _message(request, None, "A meghívó nem érvényes",
                        "A meghívó lejárt, visszavonták, vagy már felhasználták. Kérj újat.", "/belepes", 404)
    return _render(request, "invite.html", email=email, token=token)


LOGIN_COOKIE = "dwyt_signin"


@app.get("/auth/google/start")
def google_start(request: Request, meghivo: str = ""):
    settings = get_settings()
    if (limited := _limited(request, "google-start")) is not None:
        return limited
    if not settings.google_client_id:
        return _redirect("/belepes?hiba=google-nincs")
    try:
        with db.system_session() as s:
            url, state = accounts.start(s, settings, meghivo[:100] or None)
    except accounts.SignInError:
        return _redirect("/belepes?hiba=google-nincs")
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
    if browser_state and secrets.compare_digest(browser_state, state):
        with db.system_session() as s:
            pending = s.get(LoginState, security.token_hash(state)) if len(state) < 200 else None
            purpose = pending.purpose if pending is not None else "login"
        if purpose == "youtube":
            return await _youtube_callback(request, state, code)

    def sign_in() -> str:
        with db.system_session() as s:  # consumed even when the rest fails
            login = accounts.consume_state(s, state)
        if login.purpose != "login":
            raise accounts.SignInError("A belépés lejárt vagy érvénytelen. Kezdd újra.")
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


async def _youtube_callback(request: Request, state: str, code: str):
    """The end of connecting YouTube (Channels page): only for the signed-in user who started it."""
    viewer = await current_viewer(request)
    settings = get_settings()

    def connect() -> None:
        with db.system_session() as s:
            login = accounts.consume_state(s, state)
        if login.user_id != viewer.id:
            raise youtube.YoutubeError("Az összekapcsolást ugyanazzal a fiókkal kell befejezni, amelyikkel elkezdted.")
        refresh_token, email = youtube.connect_finish(login, settings, code)
        subscriptions.save_link(viewer.id, refresh_token, email)

    try:
        await run_in_threadpool(connect)
    except (accounts.SignInError, youtube.YoutubeError, secretbox.NoKey) as exc:
        response = _message(request, viewer, "A YouTube-fiók összekapcsolása nem sikerült", str(exc), "/csatornak",
                            400)
        response.delete_cookie(LOGIN_COOKIE, path="/auth/google")
        return response
    try:
        await run_in_threadpool(subscriptions.sync_google, settings, viewer.id)
    except (youtube.YoutubeError, secretbox.NoKey):
        pass  # shown on the Channels page
    response = _redirect("/csatornak")
    response.delete_cookie(LOGIN_COOKIE, path="/auth/google")
    return response


@app.get("/admin-belepes", response_class=HTMLResponse)
def admin_login_page(request: Request):
    return _render(request, "admin_login.html", step="password")


@app.post("/admin-belepes")
async def admin_login(request: Request):
    if (limited := _limited(request, "admin-login")) is not None:
        return limited
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
                log.warning("admin password login failed from %s", _client_ip(request))
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
    if (limited := _limited(request, "admin-login")) is not None:
        return limited
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
                log.warning("admin code login failed from %s", _client_ip(request))
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

_title = library.item_title


def _thumb(source: Source) -> str | None:
    if source.kind == "youtube":
        return f"https://i.ytimg.com/vi/{source.key.split(':', 1)[1]}/mqdefault.jpg"
    return None


templates.env.globals.update(item_title=_title, thumb=_thumb)

LIBRARY_FILTERS = {"": "Minden elem", "nyilvanos": "Videók, weboldalak", "sajat": "Saját felvételek",
                   "nincs-osszefoglalo": "Összefoglaló nélkül", "folyamatban": "Folyamatban"}


def _like(q: str) -> str:
    return "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


@app.get("/", response_class=HTMLResponse)
def library_page(request: Request, oldal: int = 1, q: str = "", szures: str = "",
                 viewer: Viewer = Depends(current_viewer)):
    q = q.strip()[:200]
    szures = szures if szures in LIBRARY_FILTERS else ""
    with db.user_session(viewer.id) as s:
        active = set(s.scalars(select(Job.item_id).where(Job.state.in_(ACTIVE))).all())
        query = select(Item).join(Source, Source.id == Item.source_id)
        if q:
            like = _like(q)
            query = query.where(or_(Item.title.ilike(like), Source.meta["title"].astext.ilike(like),
                                    Source.meta["channel"].astext.ilike(like), Source.url.ilike(like)))
        if szures == "nyilvanos":
            query = query.where(Item.is_public.is_(True))
        elif szures == "sajat":
            query = query.where(Item.is_public.is_(False))
        elif szures == "nincs-osszefoglalo":
            query = query.where(Item.current_summary_id.is_(None))
        elif szures == "folyamatban":
            query = query.where(Item.id.in_(active or {0}))
        total = s.scalar(select(func.count()).select_from(query.subquery())) or 0
        pages = max(1, -(-total // PAGE_SIZE))
        page = min(max(1, oldal), pages)
        found = s.scalars(query.order_by(Item.created_at.desc(), Item.id.desc())
                          .offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE)).all()
        mains = {t.id: t for t in s.scalars(select(Transcript).where(
            Transcript.id.in_([i.main_transcript_id for i in found if i.main_transcript_id])))}
        previews = {sm.id: render.plain_preview(sm.text) for sm in s.scalars(select(Summary).where(
            Summary.id.in_([i.current_summary_id for i in found if i.current_summary_id])))}
        has_runner = s.scalar(select(func.count()).select_from(Runner).where(Runner.revoked_at.is_(None))) or 0
    return _render(request, "library.html", viewer, items=found, active=active, page=page, pages=pages, total=total,
                   has_runner=has_runner, q=q, szures=szures, filters=LIBRARY_FILTERS, mains=mains,
                   previews=previews)


def _runners(s) -> list[Runner]:
    return list(s.scalars(select(Runner).where(Runner.revoked_at.is_(None)).order_by(Runner.id)))


def _model_choices(runners: list[Runner], item: Item) -> list[str]:
    return jobs.offered_models(runners, "public" if item.is_public else "private")


def _status_context(s, item: Item) -> dict[str, Any]:
    item_jobs = s.scalars(select(Job).where(Job.item_id == item.id).order_by(Job.id.desc()).limit(10)).all()
    active_jobs = [j for j in reversed(item_jobs) if j.state in ACTIVE]
    failed = next((j for j in item_jobs if j.state in ("failed", "done", "cancelled")), None)
    return {"active_jobs": active_jobs, "last_failed": failed if failed is not None and failed.state == "failed"
            else None, "now": utcnow()}


@app.get("/elem/{item_id}", response_class=HTMLResponse)
def item_page(request: Request, item_id: int, atirat: int | None = None, nezet: str = "",
              osszefoglalo: int | None = None, viewer: Viewer = Depends(current_viewer)):
    with db.user_session(viewer.id) as s:
        item = s.get(Item, item_id)
        if item is None:
            return _message(request, viewer, "Nincs ilyen elem", "Ez az elem nem létezik.", "/", 404)
        transcripts = s.scalars(select(Transcript).join(ItemTranscript, ItemTranscript.transcript_id == Transcript.id)
                                .where(ItemTranscript.item_id == item.id).order_by(Transcript.id)).all()
        chosen = next((t for t in transcripts if t.id == (atirat or item.main_transcript_id)), None)
        if chosen is None and transcripts:
            chosen = transcripts[-1]
        main = next((t for t in transcripts if t.id == item.main_transcript_id), None)
        versions = items.summary_versions(s, item.id)
        summary, summary_reused = next(((v, r) for v, r in versions if v.id == osszefoglalo), (None, False))
        if summary is None:
            summary, summary_reused = next(((v, r) for v, r in versions if v.id == item.current_summary_id),
                                           (None, False))
        translation = None
        if chosen is not None:
            translation = s.get(Translation, items.latest_translation(s, item.id, chosen.id) or 0)
            if chosen.id == item.main_transcript_id and item.current_translation_id:
                translation = s.get(Translation, item.current_translation_id) or translation
        hu = {g.idx: g.text for g in translation.segments} if translation else None
        paragraphs = library.with_translation(library.turns(chosen.segments), hu) if chosen else []
        main_paragraphs = paragraphs if main is chosen else (library.turns(main.segments) if main else [])
        links = {p["start"]: library.timestamp_url(item.source, p["start"]) for p in paragraphs}
        reused = {
            "transcript": {t for t, r in s.execute(select(ItemTranscript.transcript_id, ItemTranscript.reused)
                                                   .where(ItemTranscript.item_id == item.id)) if r},
            "translation": bool(translation and s.scalar(select(ItemTranslation.reused).where(
                ItemTranslation.item_id == item.id, ItemTranslation.translation_id == translation.id))),
        }
        status = _status_context(s, item)
        runners = _runners(s)
        can_rerun = item.source.kind != "upload" or _own_file_of(s, item) is not None
    summary_html = None
    if summary is not None:
        starts = sorted(p["start"] for p in main_paragraphs)

        def link(seconds: int) -> tuple[str, bool]:
            url = library.timestamp_url(item.source, seconds)
            if url:
                return url, True
            anchor = max((st for st in starts if st <= seconds + 0.5), default=starts[0] if starts else 0)
            return f"/elem/{item.id}#t{int(anchor)}", False
        summary_html = render.summary_html(summary.text, link)
    translating = any(j.kind == "translate" for j in status["active_jobs"])
    summarizing = any(j.kind == "summarize" for j in status["active_jobs"])
    view = nezet if nezet in ("eredeti", "magyar", "mindketto") else ("mindketto" if translation else "eredeti")
    if not translation:
        view = "eredeti"
    return _render(request, "item.html", viewer, item=item, transcripts=transcripts, transcript=chosen,
                   paragraphs=paragraphs, links=links, summary=summary, summary_html=summary_html,
                   summary_reused=summary_reused, summary_versions=versions, translation=translation,
                   view=view, reused=reused, translating=translating, summarizing=summarizing,
                   model_choices=_model_choices(runners, item), modes=MODES, cloud_models=CLOUD_MODELS,
                   local_models=LOCAL_MODELS, can_rerun=can_rerun,
                   can_translate=bool(chosen and jobs.needs_translation(chosen.language)), **status)


@app.get("/elem/{item_id}/allapot", response_class=HTMLResponse)
def item_status(request: Request, item_id: int, viewer: Viewer = Depends(current_viewer)):
    """Polled while the item has active jobs: the notice again, or a page refresh once they are done."""
    with db.user_session(viewer.id) as s:
        item = s.get(Item, item_id)
        if item is None:
            return PlainTextResponse("", status_code=404)
        status = _status_context(s, item)
    if not status["active_jobs"]:
        return Response(status_code=200, headers={"HX-Refresh": "true"})
    return _render(request, "_item_status.html", viewer, item=item, **status)


@app.post("/elem/{item_id}/torles")
async def delete_item(item_id: int, viewer: Viewer = Depends(authed_post)):
    def remove() -> None:
        with db.user_session(viewer.id) as s:
            item = s.get(Item, item_id)
            if item is not None:
                s.delete(item)
    await run_in_threadpool(remove)
    return _redirect("/")


def _chosen_model(form, runners: list[Runner], item: Item) -> tuple[str | None, str | None]:
    """(model or None for the runner's default, error)."""
    model = str(form.get("model") or "").strip()
    if not model:
        return None, None
    if model not in _model_choices(runners, item):
        return None, "Ezt a modellt egyik futtatód sem ajánlja fel ehhez az elemhez."
    return model, None


@app.post("/elem/{item_id}/osszefoglalo")
async def resummarize(request: Request, item_id: int, viewer: Viewer = Depends(authed_post)):
    form = await request.form()

    def enqueue() -> str | None:
        with db.user_session(viewer.id) as s:
            item = s.get(Item, item_id)
            if item is None or item.main_transcript_id is None:
                return "Ehhez az elemhez még nincs átirat."
            model, problem = _chosen_model(form, _runners(s), item)
            if problem:
                return problem
            busy = s.scalar(select(Job.id).where(Job.item_id == item.id, Job.kind == "summarize",
                                                 Job.state.in_(ACTIVE)))
            if busy:
                return "Az összefoglaló már készül."
            jobs.enqueue_summary(s, viewer.id, item, item.main_transcript_id, _title(item), model=model)
            return None
    error = await run_in_threadpool(enqueue)
    if error:
        return _message(request, viewer, "Az összefoglaló nem indult el", error, f"/elem/{item_id}", 422)
    return _redirect(f"/elem/{item_id}")


@app.post("/elem/{item_id}/osszefoglalo/aktualis")
async def choose_summary(request: Request, item_id: int, viewer: Viewer = Depends(authed_post)):
    raw = str((await request.form()).get("summary_id") or "")

    def change() -> bool:
        with db.user_session(viewer.id) as s:
            item = s.get(Item, item_id)
            return item is not None and raw.isdigit() and items.set_current_summary(s, item, int(raw))
    if not await run_in_threadpool(change):
        return _message(request, viewer, "Nincs ilyen összefoglaló", "Ez az összefoglaló nem ehhez az elemhez "
                        "tartozik.", f"/elem/{item_id}", 404)
    return _redirect(f"/elem/{item_id}")


def _download(body: str, filename: str, ascii_name: str, media_type: str) -> Response:
    return PlainTextResponse(body, media_type=f"{media_type}; charset=utf-8", headers={
        "Content-Disposition": f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename)}"})


@app.get("/elem/{item_id}/osszefoglalo/{summary_id}.md")
def download_summary(item_id: int, summary_id: int, viewer: Viewer = Depends(current_viewer)):
    with db.user_session(viewer.id) as s:
        item = s.get(Item, item_id)
        if item is None or s.get(ItemSummary, (item_id, summary_id)) is None:
            return PlainTextResponse("Nincs ilyen összefoglaló.", status_code=404)
        body = library.summary_markdown(item, s.get(Summary, summary_id))
        name = library.file_base(item, "osszefoglalo") + ".osszefoglalo.md"
    return _download(body, name, "osszefoglalo.md", "text/markdown")


@app.get("/elem/{item_id}/atirat/{transcript_id}.{fmt}")
def download_transcript(item_id: int, transcript_id: int, fmt: str, nyelv: str = "",
                        viewer: Viewer = Depends(current_viewer)):
    if fmt not in ("txt", "srt"):
        return PlainTextResponse("Ismeretlen formátum.", status_code=404)
    with db.user_session(viewer.id) as s:
        item = s.get(Item, item_id)
        if item is None or s.get(ItemTranscript, (item_id, transcript_id)) is None:
            return PlainTextResponse("Nincs ilyen átirat.", status_code=404)
        t = s.get(Transcript, transcript_id)
        hu = None
        if nyelv == "hu":
            tid = items.latest_translation(s, item_id, transcript_id)
            if transcript_id == item.main_transcript_id and item.current_translation_id:
                tid = item.current_translation_id
            if tid is None:
                return PlainTextResponse("Ennek az átiratnak nincs magyar fordítása.", status_code=404)
            hu = {g.idx: g.text for g in s.get(Translation, tid).segments}
        body = library.to_txt(item, t, hu) if fmt == "txt" else library.to_srt(t, hu)
        name = f"{library.file_base(item, 'atirat')}.{'hu' if hu is not None else t.language or 'xx'}.{fmt}"
    return _download(body, name, f"atirat.{fmt}", "text/plain")


@app.post("/elem/{item_id}/fo")
async def make_main(request: Request, item_id: int, viewer: Viewer = Depends(authed_post)):
    raw = str((await request.form()).get("transcript_id") or "")

    def change() -> bool:
        with db.user_session(viewer.id) as s:
            item = s.get(Item, item_id)
            return item is not None and raw.isdigit() and items.set_main(s, viewer.id, item, int(raw))
    if not await run_in_threadpool(change):
        return _message(request, viewer, "Nincs ilyen átirat", "Ez az átirat nem ehhez az elemhez tartozik.",
                        f"/elem/{item_id}", 404)
    return _redirect(f"/elem/{item_id}")


@app.post("/elem/{item_id}/atirat/{transcript_id}/torles")
async def delete_transcript(item_id: int, transcript_id: int, viewer: Viewer = Depends(authed_post)):
    def remove() -> None:
        with db.user_session(viewer.id) as s:
            item = s.get(Item, item_id)
            if item is not None:
                items.remove_transcript(s, viewer.id, item, transcript_id)
    await run_in_threadpool(remove)
    return _redirect(f"/elem/{item_id}")


def _own_file_of(s, item: Item) -> dict[str, Any] | None:
    """The watched-folder file of an own recording (the runner that registered it still has it);
    browser uploads are gone from here once transcribed."""
    for job in s.scalars(select(Job).where(Job.item_id == item.id, Job.kind == "transcribe")
                         .order_by(Job.id.desc())):
        src = (job.payload or {}).get("source") or {}
        if src.get("kind") == "local":
            return dict(src)
    return None


@app.post("/elem/{item_id}/sajat")
async def rerun_own(request: Request, item_id: int, viewer: Viewer = Depends(authed_post)):
    """Make the main transcript again with the user's own runner (e.g. instead of a reused one)."""
    def enqueue() -> str | None:
        with db.user_session(viewer.id) as s:
            item = s.get(Item, item_id)
            if item is None or item.main_transcript_id is None:
                return "Ehhez az elemhez még nincs átirat."
            if item.source.kind == "upload":
                return "Saját felvételt csak a fájllal együtt lehet újra átíratni."
            busy = s.scalar(select(Job.id).where(Job.item_id == item.id, Job.kind == "transcribe",
                                                 Job.state.in_(ACTIVE)))
            if busy:
                return "Az átírás már folyamatban van."
            t = s.get(Transcript, item.main_transcript_id)
            job = jobs.enqueue_transcription(s, viewer.id, item, item.source, dict(t.options or {}),
                                             summarize=item.current_summary_id is not None)
            job.payload = {**job.payload, "make_main": True}
            return None
    error = await run_in_threadpool(enqueue)
    if error:
        return _message(request, viewer, "Az átírás nem indult el", error, f"/elem/{item_id}", 422)
    return _redirect(f"/elem/{item_id}")


@app.post("/elem/{item_id}/ujrairas")
async def retranscribe(request: Request, item_id: int, viewer: Viewer = Depends(authed_post)):
    """A new transcript of the item with other settings (mode, model, language, speakers)."""
    form = {k: str(v) for k, v in (await request.form()).items()}
    options, errors = parse_options(form)
    make_main = form.get("make_main") == "on"
    summarize = form.get("summarize") == "on"

    def enqueue() -> str | None:
        with db.system_session() as s:
            item_source = s.scalar(select(Source).join(Item, Item.source_id == Source.id)
                                   .where(Item.id == item_id, Item.user_id == viewer.id))
            if item_source is not None:
                s.expunge(item_source)
        if item_source is None:
            return "Nincs ilyen elem."
        if errors:
            return " ".join(errors)
        if options.get("mode") == "captions" and item_source.kind != "youtube":
            return "YouTube-feliratot csak YouTube-videóhoz lehet kérni."
        try:
            ownfiles.check_storage(get_settings(), viewer.id)
        except ownfiles.QuotaError as exc:
            return str(exc)
        found = reuse.find(item_source, options)
        with db.user_session(viewer.id) as s:
            item = s.get(Item, item_id)
            busy = s.scalar(select(Job.id).where(Job.item_id == item.id, Job.kind == "transcribe",
                                                 Job.state.in_(ACTIVE)))
            if busy:
                return "Az elemhez már fut egy átírás; várd meg, vagy szakítsd meg a Munkák oldalon."
            if found is not None:
                if s.get(ItemTranscript, (item.id, found.transcript_id)) is not None:
                    return "Ilyen beállításokkal már van átirata az elemnek."
                s.add(ItemTranscript(item_id=item.id, transcript_id=found.transcript_id, user_id=viewer.id,
                                     reused=True))
                for model, key, ident in ((ItemSummary, "summary_id", found.summary_id),
                                          (ItemTranslation, "translation_id", found.translation_id)):
                    if ident and s.get(model, (item.id, ident)) is None:
                        s.add(model(item_id=item.id, user_id=viewer.id, reused=True, **{key: ident}))
                s.flush()
                if make_main or item.main_transcript_id is None:
                    items.set_main(s, viewer.id, item, found.transcript_id, want_summary=summarize)
                return None
            file = None
            if item_source.kind == "upload":
                file = _own_file_of(s, item)
                if file is None:
                    return ("A feltöltött fájl már nincs a központon. Töltsd fel újra az Új átirat oldalon: "
                            "az új átirat ehhez az elemhez kerül.")
                if options.get("mode") == "captions":
                    return "Saját felvételhez YouTube-felirat nem kérhető."
            job = jobs.enqueue_transcription(s, viewer.id, item, s.get(Source, item_source.id), options, summarize,
                                             file=file)
            if make_main:
                job.payload = {**job.payload, "make_main": True}
            return None
    error = await run_in_threadpool(enqueue)
    if error:
        return _message(request, viewer, "Az újraírás nem indult el", error, f"/elem/{item_id}", 422)
    return _redirect(f"/elem/{item_id}")


@app.post("/elem/{item_id}/forditas")
async def request_translation(request: Request, item_id: int, viewer: Viewer = Depends(authed_post)):
    form = await request.form()

    def enqueue() -> str | None:
        with db.user_session(viewer.id) as s:
            item = s.get(Item, item_id)
            if item is None or item.main_transcript_id is None:
                return "Ehhez az elemhez még nincs átirat."
            t = s.get(Transcript, item.main_transcript_id)
            if not jobs.needs_translation(t.language):
                return "Az átirat magyar, nincs mit fordítani."
            model, problem = _chosen_model(form, _runners(s), item)
            if problem:
                return problem
            busy = s.scalar(select(Job.id).where(Job.item_id == item.id, Job.kind == "translate",
                                                 Job.state.in_(ACTIVE)))
            if busy:
                return "A fordítás már készül."
            jobs.enqueue_translation(s, viewer.id, item, item.main_transcript_id, _title(item), model=model)
            return None
    error = await run_in_threadpool(enqueue)
    if error:
        return _message(request, viewer, "A fordítás nem indult el", error, f"/elem/{item_id}", 422)
    return _redirect(f"/elem/{item_id}")


# ---- subscriptions --------------------------------------------------------------------------

INBOX_TABS = {"uj": ("new",), "folyamatban": ("queued",), "kesz": ("done",), "kihagyott": ("skipped", "failed"),
              "shorts": ("short",)}
INBOX_LABELS = {"uj": "Új", "folyamatban": "Folyamatban", "kesz": "Kész", "kihagyott": "Kihagyott",
                "shorts": "Shorts"}


def _channels_context(viewer: Viewer, message: str | None = None, error: bool = False) -> dict[str, Any]:
    settings = get_settings()
    with db.user_session(viewer.id) as s:
        subs = s.scalars(select(Subscription).join(Channel, Channel.id == Subscription.channel_id)
                         .order_by(Subscription.active.desc(), func.lower(func.coalesce(Channel.title,
                                                                                          Channel.id)))).all()
        new_counts = dict(s.execute(select(InboxEntry.channel_id, func.count()).where(InboxEntry.state == "new")
                                    .group_by(InboxEntry.channel_id)).all())
    return {"subs": subs, "new_counts": new_counts, "rules": subscriptions.RULES, "sources": subscriptions.SOURCES,
            "notify_options": subscriptions.NOTIFY, "link": subscriptions.link_of(viewer.id),
            "google_ready": bool(settings.google_client_id and settings.google_client_secret),
            "token_ready": secretbox.available(), "message": message, "message_error": error,
            "poll_min": settings.feed_poll_min}


@app.get("/csatornak", response_class=HTMLResponse)
def channels_page(request: Request, viewer: Viewer = Depends(current_viewer)):
    return _render(request, "channels.html", viewer, **_channels_context(viewer))


def _channels_message(request: Request, viewer: Viewer, message: str, error: bool = False, status: int = 200):
    return _render(request, "channels.html", viewer, status_code=status,
                   **_channels_context(viewer, message, error))


@app.post("/csatornak/youtube")
async def youtube_connect(request: Request, viewer: Viewer = Depends(authed_post)):
    settings = get_settings()
    if not secretbox.available():
        return _channels_message(request, viewer, "A YouTube-fiók összekapcsolásához a központon be kell állítani a "
                                 "TOKEN_KEY-t (docs/telepites.md).", True, 422)

    def start() -> tuple[str, str]:
        with db.system_session() as s:
            return youtube.connect_start(s, settings, viewer.id, viewer.email)
    try:
        url, state = await run_in_threadpool(start)
    except youtube.YoutubeError as exc:
        return _channels_message(request, viewer, str(exc), True, 422)
    response = RedirectResponse(url, status_code=303)
    response.set_cookie(LOGIN_COOKIE, state, max_age=youtube.STATE_MINUTES * 60, httponly=True, samesite="lax",
                        secure=settings.cookie_secure, path="/auth/google")
    return response


@app.post("/csatornak/youtube/frissites")
async def youtube_sync(request: Request, viewer: Viewer = Depends(authed_post)):
    try:
        got = await run_in_threadpool(subscriptions.sync_google, get_settings(), viewer.id)
    except (youtube.YoutubeError, secretbox.NoKey) as exc:
        return _channels_message(request, viewer, str(exc), True, 502)
    return _channels_message(request, viewer, f"A YouTube-fiókban {got['count']} feliratkozás van; "
                             f"{got['added']} új csatorna, {got['gone']} leiratkozott.")


@app.post("/csatornak/youtube/levalasztas")
async def youtube_unlink(viewer: Viewer = Depends(authed_post)):
    await run_in_threadpool(subscriptions.unlink, get_settings(), viewer.id)
    return _redirect("/csatornak")


@app.post("/csatornak/hozzaad")
async def channel_add(request: Request, viewer: Viewer = Depends(authed_post)):
    given = str((await request.form()).get("channel") or "")

    def add() -> str:
        cid, title = youtube.resolve_channel(get_settings(), given)
        added = subscriptions.add_subscriptions(viewer.id, [(cid, title)], "manual")
        return f"{'Felvéve' if added else 'Már követed'}: {title or cid}. Az ezután megjelenő videói kerülnek a " \
               "postaládába."
    try:
        message = await run_in_threadpool(add)
    except youtube.YoutubeError as exc:
        return _channels_message(request, viewer, str(exc), True, 422)
    return _channels_message(request, viewer, message)


@app.post("/csatornak/takeout")
async def channel_takeout(request: Request, viewer: Viewer = Depends(authed_post)):
    upload = (await request.form()).get("file")
    data = await upload.read(5 * 1024 ** 2 + 1) if hasattr(upload, "read") else b""
    if len(data) > 5 * 1024 ** 2:
        return _channels_message(request, viewer, "A fájl túl nagy (legfeljebb 5 MB).", True, 413)
    found = youtube.parse_takeout(data)
    if not found:
        return _channels_message(request, viewer, "A fájlban nincs csatorna. A Google Takeoutban a YouTube "
                                 "„subscriptions.csv” fájlját add meg.", True, 422)
    added = await run_in_threadpool(subscriptions.add_subscriptions, viewer.id, found, "takeout")
    return _channels_message(request, viewer, f"{len(found)} csatorna a fájlban, ebből {added} új.")


@app.post("/csatornak/ellenorzes")
async def channels_poll_now(request: Request, viewer: Viewer = Depends(authed_post)):
    def run() -> int:
        with db.user_session(viewer.id) as s:
            ids = s.scalars(select(Subscription.channel_id).where(Subscription.active.is_(True),
                                                                  Subscription.rule != "never").limit(100)).all()
        return sum(subscriptions.poll_channel(get_settings(), cid) for cid in ids)
    new = await run_in_threadpool(run)
    return _channels_message(request, viewer, f"Ellenőrizve; {new} új videó a hírcsatornákban.")


@app.post("/csatornak/{sub_id}/beallitas")
async def channel_settings(sub_id: int, request: Request, viewer: Viewer = Depends(authed_post)):
    form = await request.form()
    ok = await run_in_threadpool(subscriptions.update_subscription, viewer.id, sub_id, str(form.get("rule") or ""),
                                 form.get("allow_cloud") == "on", str(form.get("notify") or "digest"))
    if not ok:
        return _channels_message(request, viewer, "Nincs ilyen csatorna vagy beállítás.", True, 422)
    return _redirect(f"/csatornak#c{sub_id}")


@app.post("/csatornak/{sub_id}/torles")
async def channel_remove(sub_id: int, viewer: Viewer = Depends(authed_post)):
    await run_in_threadpool(subscriptions.remove_subscription, viewer.id, sub_id)
    return _redirect("/csatornak")


@app.get("/postalada", response_class=HTMLResponse)
def inbox_page(request: Request, allapot: str = "uj", viewer: Viewer = Depends(current_viewer)):
    tab = allapot if allapot in INBOX_TABS else "uj"
    with db.user_session(viewer.id) as s:
        subscriptions.sync_inbox(s)
        entries = s.scalars(select(InboxEntry).where(InboxEntry.state.in_(INBOX_TABS[tab]))
                            .order_by(InboxEntry.published_at.desc().nulls_last(), InboxEntry.id.desc())
                            .limit(200)).all()
        counts = dict(s.execute(select(InboxEntry.state, func.count()).group_by(InboxEntry.state)).all())
        job_map = {j.id: j for j in s.scalars(select(Job).where(Job.id.in_([e.job_id for e in entries if e.job_id])))}
        waiting = subscriptions.gpu_waiting(s)
        followed = s.scalar(select(func.count()).select_from(Subscription)) or 0
    tab_counts = {k: sum(counts.get(st, 0) for st in v) for k, v in INBOX_TABS.items()}
    return _render(request, "inbox.html", viewer, entries=entries, tab=tab, tabs=INBOX_LABELS, tab_counts=tab_counts,
                   gpu_waiting=waiting, job_map=job_map, followed=followed, now=utcnow())


@app.post("/postalada/{entry_id}/keres")
async def inbox_request(request: Request, entry_id: int, viewer: Viewer = Depends(authed_post)):
    back = str((await request.form()).get("vissza") or "uj")
    await run_in_threadpool(subscriptions.request_entry, get_settings(), viewer.id, entry_id)
    return _redirect(f"/postalada?allapot={back if back in INBOX_TABS else 'uj'}")


@app.post("/postalada/{entry_id}/kihagy")
async def inbox_skip(entry_id: int, viewer: Viewer = Depends(authed_post)):
    await run_in_threadpool(subscriptions.skip, viewer.id, [entry_id])
    return _redirect("/postalada")


@app.post("/postalada/kihagy-mind")
async def inbox_skip_all(viewer: Viewer = Depends(authed_post)):
    await run_in_threadpool(subscriptions.skip, viewer.id, None)
    return _redirect("/postalada")


# ---- settings -----------------------------------------------------------------------------

def _usage(viewer: Viewer) -> dict[str, Any]:
    settings = get_settings()
    lim = ownfiles.user_limits(settings, viewer.id)
    with db.user_session(viewer.id) as s:
        pending = ownfiles.pending_uploads(s, viewer.id)
    return {"used_mb": ownfiles.storage_used(viewer.id) / 1024 ** 2, "quota_mb": lim.storage_bytes / 1024 ** 2,
            "upload_mb": lim.upload_bytes // 1024 ** 2, "pending": pending, "pending_max": lim.pending}


@app.get("/beallitasok", response_class=HTMLResponse)
def settings_page(request: Request, viewer: Viewer = Depends(current_viewer)):
    with db.system_session() as s:
        user = s.get(User, viewer.id)
        translate_auto = user.translate_auto
    return _render(request, "settings.html", viewer, translate_auto=translate_auto, saved=False, usage=_usage(viewer))


@app.post("/beallitasok")
async def save_settings(request: Request, viewer: Viewer = Depends(authed_post)):
    form = await request.form()
    value = form.get("translate_auto") == "on"

    def save() -> None:
        with db.system_session() as s:  # the users table is the system's; only the viewer's own row
            s.get(User, viewer.id).translate_auto = value
    await run_in_threadpool(save)
    return _render(request, "settings.html", viewer, translate_auto=value, saved=True, usage=_usage(viewer))


# ---- search -------------------------------------------------------------------------------

def _date(value: str) -> date | None:
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None


@app.get("/kereses", response_class=HTMLResponse)
def search_page(request: Request, q: str = "", pontos: str = "", csatorna: str = "", nyelv: str = "", mod: str = "",
                tol: str = "", ig: str = "", viewer: Viewer = Depends(current_viewer)):
    filters = search.Filters(channel=csatorna[:300], language=nyelv[:10], mode=mod if mod in MODES else "",
                             date_from=_date(tol), date_to=_date(ig))
    outcome = search.run(viewer.id, q, get_settings(), exact=pontos == "on", filters=filters) if q.strip() else None
    links = {}
    if outcome:
        for r in outcome.results:
            for h in r.hits:
                if h.start_s is not None and r.item["kind"] == "youtube":
                    video = r.item["key"].split(":", 1)[1]
                    links[(r.item["id"], h.chunk_id)] = f"https://www.youtube.com/watch?v={video}&t={int(h.start_s)}s"
    return _render(request, "search.html", viewer, q=q, exact=pontos == "on", outcome=outcome, filters=filters,
                   channels=search.channels(viewer.id), modes=library.MODE_LABELS, links=links,
                   snippet=search.snippet, best_text=search.best_text, index_status=indexing.status(viewer.id))


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


def _submit_ctx(viewer: Viewer) -> dict[str, Any]:
    lim = ownfiles.user_limits(get_settings(), viewer.id)
    return {"modes": MODES, "cloud_models": CLOUD_MODELS, "local_models": LOCAL_MODELS,
            "upload_max_mb": lim.upload_bytes // 1024 ** 2}


@app.get("/uj", response_class=HTMLResponse)
def submit_page(request: Request, viewer: Viewer = Depends(current_viewer)):
    return _render(request, "submit.html", viewer, form={"summarize": "on", "source": "url"}, errors=[],
                   **_submit_ctx(viewer))


@app.put("/feltoltes")
async def stage_upload(request: Request, name: str = "feltoltes", viewer: Viewer = Depends(current_viewer)):
    """Receive a file from the browser (raw body, X-CSRF-Token header) into staging."""
    if not secrets.compare_digest(request.headers.get("X-CSRF-Token", ""), viewer.csrf):
        raise BadCsrf()
    try:
        up = await ownfiles.receive(request, get_settings(), viewer.id, name)
    except ownfiles.QuotaError as exc:
        return JSONResponse({"error": str(exc)}, status_code=413)
    return {"staging_id": up.id, "name": up.name, "size": up.size}


def _source_for(ref: sources.SourceRef) -> int:
    with db.system_session() as s:
        s.execute(insert(Source).values(kind=ref.kind, key=ref.key, url=ref.url, meta={})
                  .on_conflict_do_nothing(index_elements=[Source.key]))
        return s.scalar(select(Source.id).where(Source.key == ref.key))


@app.post("/uj")
async def submit(request: Request, viewer: Viewer = Depends(authed_post)):
    form = {k: str(v) for k, v in (await request.form()).items()}
    if form.get("source") == "upload":
        return await _submit_upload(request, viewer, form)
    options, errors = parse_options(form)
    ref = None
    try:
        ref = sources.parse(form.get("url", ""))
    except sources.BadUrl as exc:
        errors.append(str(exc))
    if ref is not None and options.get("mode") == "captions" and ref.kind != "youtube":
        errors.append("YouTube-feliratot csak YouTube-videóhoz lehet kérni.")
    if not errors:
        try:
            await run_in_threadpool(ownfiles.check_storage, get_settings(), viewer.id)
        except ownfiles.QuotaError as exc:
            errors.append(str(exc))
    if errors:
        return _render(request, "submit.html", viewer, form=form, errors=errors, status_code=422,
                       **_submit_ctx(viewer))
    source_id = await run_in_threadpool(_source_for, ref)
    summarize = form.get("summarize") == "on"

    def enqueue() -> int:
        with db.system_session() as s:
            source = s.get(Source, source_id)
            s.expunge(source)
        found = reuse.find(source, options)
        auto_translate = reuse.translate_auto(viewer.id)
        with db.user_session(viewer.id) as s:
            item = s.scalar(select(Item).where(Item.source_id == source_id))
            if item is None:
                item = Item(user_id=viewer.id, source_id=source_id, is_public=True)
                s.add(item)
                s.flush()
                indexing.enqueue(s, item)  # title and channel are searchable at once
            if found is not None:
                reuse.attach(s, viewer.id, item, found, summarize, auto_translate)
            else:
                jobs.enqueue_transcription(s, viewer.id, item, s.get(Source, source_id), options, summarize)
            return item.id

    item_id = await run_in_threadpool(enqueue)
    return _redirect(f"/elem/{item_id}")


async def _submit_upload(request: Request, viewer: Viewer, form: dict[str, str]):
    """An uploaded own recording: private, never reused, local Whisper by default."""
    form.setdefault("mode", "local")
    options, errors = parse_options(form)
    if options.get("mode") == "captions":
        errors.append("Feltöltött fájlhoz YouTube-felirat nem kérhető; válaszd a helyi Whispert vagy a felhőt.")
    staging_id = form.get("staging_id", "")

    def prepare() -> tuple[Upload | None, str | None]:
        if not staging_id.isdigit():
            return None, "Előbb töltsd fel a fájlt (várd meg, amíg a feltöltés befejeződik)."
        with db.user_session(viewer.id) as s:
            up = s.get(Upload, int(staging_id))
            if up is None or up.used_at is not None or up.consumed_at is not None:
                return None, "Ez a feltöltés már nem használható; töltsd fel újra."
            s.expunge(up)
            return up, None
    up, problem = await run_in_threadpool(prepare)
    if problem:
        errors.append(problem)
    if errors:
        return _render(request, "submit.html", viewer, form=form, errors=errors, status_code=422,
                       **_submit_ctx(viewer))
    summarize = form.get("summarize") == "on"
    key = f"upload:{up.sha256}"

    def enqueue() -> int:
        with db.system_session() as s:
            s.execute(insert(Source).values(kind="upload", key=key, url=None, meta={})
                      .on_conflict_do_nothing(index_elements=[Source.key]))
            source_id = s.scalar(select(Source.id).where(Source.key == key))
        with db.user_session(viewer.id) as s:
            item = s.scalar(select(Item).where(Item.source_id == source_id))
            if item is None:
                item = Item(user_id=viewer.id, source_id=source_id, is_public=False, title=up.name)
                s.add(item)
                s.flush()
                indexing.enqueue(s, item)
            s.get(Upload, up.id).used_at = utcnow()
            jobs.enqueue_transcription(s, viewer.id, item, s.get(Source, source_id), options, summarize,
                                       file={"kind": "upload", "sha256": up.sha256, "name": up.name,
                                             "size": up.size, "upload_id": up.id})
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
             and not any(jobs.fits(j, r.capabilities or {}, r.id) for r in runners)}
    with db.user_session(viewer.id) as s:
        counts = items.backfill_counts(s)
    return {"active": active, "recent": recent, "runner_count": len(runners), "online_count": len(online),
            "unfit": unfit, "backfill": counts}


@app.get("/munkak", response_class=HTMLResponse)
def jobs_page(request: Request, viewer: Viewer = Depends(current_viewer)):
    return _render(request, "jobs.html", viewer, **_jobs_context(viewer))


@app.get("/munkak/lista", response_class=HTMLResponse)
def jobs_partial(request: Request, viewer: Viewer = Depends(current_viewer)):
    return _render(request, "_jobs.html", viewer, **_jobs_context(viewer))


BACKFILL = {"osszefoglalok": "summaries", "forditasok": "translations", "indexeles": "index"}


@app.post("/munkak/potlas/{what}")
async def backfill(what: str, viewer: Viewer = Depends(authed_post)):
    """Queue the missing summaries / translations of every item, or re-index the whole library."""
    if what not in BACKFILL:
        return PlainTextResponse("Ismeretlen pótlás.", status_code=404)

    def run() -> None:
        with db.user_session(viewer.id) as s:
            items.backfill(s, viewer.id, BACKFILL[what])
    await run_in_threadpool(run)
    return _redirect("/munkak")


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
    settings = get_settings()
    usage = {u.id: ownfiles.storage_used(u.id) / 1024 ** 2 for u in users}
    return {"users": users, "item_counts": counts, "runner_counts": runner_counts, "invites": invites,
            "now": utcnow(), "registration": settings.registration, "usage": usage,
            "default_storage_mb": settings.storage_quota_mb, "default_upload_mb": settings.upload_max_mb}


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


@app.post("/admin/felhasznalo/{user_id}/kvota")
async def user_quota(request: Request, user_id: int, viewer: Viewer = Depends(admin_post)):
    form = await request.form()

    def value(name: str) -> int | None:
        raw = str(form.get(name, "")).strip()
        return int(raw) if raw.isdigit() and 0 < int(raw) <= 1_000_000 else None

    def run() -> None:
        with db.system_session() as s:
            user = s.get(User, user_id)
            if user is not None:
                user.quota_storage_mb, user.quota_upload_mb = value("storage_mb"), value("upload_mb")
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
