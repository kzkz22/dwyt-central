"""MCP server (Streamable HTTP at /mcp) for Hermes, OpenCode, Claude and other MCP clients
(round 4c, ported from yt-transcribe-webui and made multi-user).

Every user makes named, revocable tokens on the Settings page (only the hash is stored). A token
decides what its client may do:
  - reading tools (always): kereses, atirat, osszefoglalo, legutobbiak;
  - starting tools (can_start): atiras_inditasa, munka_allapota;
  - own recordings (include_private): uploads and watched-folder recordings stay hidden otherwise,
    because the client's model may be a cloud one.
Stateless, JSON responses: every request stands alone. The caller (user and token) travels in a
context variable set by the endpoint; every query runs in that user's session (row-level security).
Tool texts are Hungarian like the UI.
"""
from __future__ import annotations

import contextlib
import contextvars
import functools
import html
import logging
import re
from dataclasses import dataclass
from datetime import date
from typing import Any, Literal

from sqlalchemy import func, select, update

from . import db, jobs, library, personal, ratelimit, search, security, sources
from . import submit as submit_mod
from .config import get_settings
from .db import utcnow
from .models import ACTIVE, Item, ItemTranscript, ItemTranslation, Job, McpToken, Summary, Transcript, Translation

log = logging.getLogger("dwyt.mcp")

MAX_TRANSCRIPT_CHARS = 60000
TOKEN_PREFIX = "dwyt_mcp_"
SOURCE_LABELS = {"youtube": "YouTube", "url": "weboldal", "upload": "saját felvétel"}
INSTRUCTIONS = (
    "A felhasználó saját videó- és hangfelvétel-könyvtára (dwyt): átiratok időbélyeggel és beszélőkkel, "
    "magyar fordítás, magyar összefoglaló. Keresésnél a `kereses` elemeket és időpontokat ad; egy elem "
    "részletei az `atirat` és az `osszefoglalo` eszközzel olvashatók. Hivatkozáskor add meg az időpontot "
    "(és ha van, a linket). Az átirat lehet automatikus YouTube-felirat is: ez pontatlanabb, beszélők nélkül."
)


@dataclass(frozen=True)
class Caller:
    user_id: int
    token_id: int
    can_start: bool
    include_private: bool


CALLER: contextvars.ContextVar[Caller | None] = contextvars.ContextVar("dwyt_mcp_caller", default=None)


class ToolFailure(Exception):
    """A user-facing error: becomes the tool's error text."""


def _caller() -> Caller:
    caller = CALLER.get()
    if caller is None:  # never reached through the endpoint; a guard for direct calls
        raise ToolFailure("Nincs bejelentkezett felhasználó.")
    return caller


# ---- helpers ------------------------------------------------------------------------------

def _link(item_id: int) -> str:
    return f"{get_settings().base_url}/elem/{item_id}"


def _visible_item(s, item_id: int) -> Item:
    item = s.get(Item, item_id)
    if item is None or (not item.is_public and not _caller().include_private):
        raise ToolFailure(f"Nincs {item_id} azonosítójú elem.")
    return item


def _plain(markup: Any) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", str(markup)))


def _date(value: str | None, name: str) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        raise ToolFailure(f"{name}: ÉÉÉÉ-HH-NN formátumú dátum kell (például 2026-10-01).") from None


def _entry(item: Item, language: str | None) -> dict[str, Any]:
    meta = item.source.meta or {}
    published = meta.get("published") or (item.created_at.date().isoformat() if item.created_at else None)
    return {"elem_id": item.id, "cim": library.item_title(item), "csatorna": meta.get("channel"),
            "datum": published, "hossz": library.hms(meta["duration_s"]) if meta.get("duration_s") else None,
            "nyelv": language, "forras": SOURCE_LABELS.get(item.source.kind, item.source.kind), "link": _link(item.id)}


# ---- reading tools ------------------------------------------------------------------------

def kereses(kerdes: str, csatorna: str | None = None, nyelv: str | None = None,
            datumtol: str | None = None, datumig: str | None = None, pontos: bool = False,
            darab: int = 10) -> dict[str, Any]:
    """Keresés a könyvtárban: szavakra (magyar szótövezéssel, ékezet nélkül is), szórészletre és
    jelentésre (más nyelvű átiratban is). Elemenként legfeljebb 3 találat időponttal és szövegrészlettel.

    kerdes: a keresett szöveg vagy kérdés. csatorna: pontos csatornanév. nyelv: az elem nyelve (hu, en…).
    datumtol/datumig: ÉÉÉÉ-HH-NN (megjelenés napja). pontos: csak szó szerinti egyezés.
    darab: legfeljebb hány elem (1–30)."""
    caller = _caller()
    if not kerdes or not kerdes.strip():
        raise ToolFailure("Üres keresés.")
    filters = search.Filters(channel=(csatorna or "").strip()[:300], language=(nyelv or "").strip().lower()[:10],
                             date_from=_date(datumtol, "datumtol"), date_to=_date(datumig, "datumig"),
                             public_only=not caller.include_private)
    outcome = search.run(caller.user_id, kerdes, get_settings(), exact=pontos, filters=filters)
    results = []
    for r in outcome.results[:max(1, min(darab, 30))]:
        item = r.item
        entry: dict[str, Any] = {"elem_id": item["id"], "cim": item["title"], "csatorna": item.get("channel"),
                                 "nyelv": item.get("language"), "link": _link(item["id"]),
                                 "forras": SOURCE_LABELS.get(item["kind"], item["kind"])}
        hits = []
        for h in r.hits:
            body, side = search.best_text(h, kerdes)
            hit: dict[str, Any] = {"resz": {"meta": "adatok", "summary": "összefoglaló"}.get(h.kind, "átirat"),
                                   "szoveg": _plain(search.snippet(body, kerdes, width=320)),
                                   "szoveg_nyelve": "magyar" if side == "magyar" else (h.language or "eredeti")}
            if h.kind == "transcript" and h.start_s is not None:
                hit["ido"] = library.hms(h.start_s)
                hit["ido_mp"] = int(h.start_s)
                if item["kind"] == "youtube":
                    hit["video_link"] = f"https://www.youtube.com/watch?v={item['key'].split(':', 1)[1]}" \
                                        f"&t={int(h.start_s)}s"
            hits.append(hit)
        entry["talalatok"] = hits
        results.append(entry)
    out: dict[str, Any] = {"talalatok_szama": len(results), "elemek": results}
    if outcome.meaning_error:
        out["megjegyzes"] = "A jelentés szerinti keresés most nem érhető el; csak szövegre keresett."
    return out


def atirat(elem_id: int, nyelv: Literal["eredeti", "magyar", "mindketto"] = "eredeti",
           tol_mp: float = 0, ig_mp: float | None = None, atirat_id: int | None = None,
           max_karakter: int = 20000) -> str:
    """Egy elem átirata időbélyegekkel és beszélőkkel, bekezdésenként.

    nyelv: eredeti | magyar (a fordítás) | mindketto. tol_mp/ig_mp: csak ez a szakasz (másodperc).
    atirat_id: másik átirat ugyanahhoz az elemhez (alapból a fő átirat; a fejléc felsorolja a többit).
    max_karakter: a válasz hossza (legfeljebb 60000); ha nem fér ki, a végén ott a folytatás tol_mp-je."""
    caller = _caller()
    limit = max(1000, min(max_karakter, MAX_TRANSCRIPT_CHARS))
    with db.user_session(caller.user_id) as s:
        item = _visible_item(s, elem_id)
        transcripts = s.scalars(select(Transcript).join(ItemTranscript, ItemTranscript.transcript_id == Transcript.id)
                                .where(ItemTranscript.item_id == item.id).order_by(Transcript.id)).all()
        if not transcripts:
            raise ToolFailure("Ennek az elemnek még nincs átirata.")
        wanted = atirat_id or item.main_transcript_id
        t = next((x for x in transcripts if x.id == wanted), None)
        if t is None:
            if atirat_id:
                raise ToolFailure(f"Ennek az elemnek nincs {atirat_id} azonosítójú átirata.")
            t = transcripts[-1]
        hu = None
        if nyelv != "eredeti":
            tr = s.scalar(select(Translation).join(ItemTranslation, ItemTranslation.translation_id == Translation.id)
                          .where(ItemTranslation.item_id == item.id, Translation.transcript_id == t.id)
                          .order_by(Translation.id.desc()).limit(1))
            if tr is not None:
                hu = {x.idx: x.text for x in tr.segments}
            elif (t.language or "").startswith("hu"):
                nyelv = "eredeti"  # a Hungarian transcript is its own Hungarian version
            else:
                raise ToolFailure("Ehhez az átirathoz még nincs magyar fordítás; kérd nyelv=eredeti értékkel.")
        meta = item.source.meta or {}
        head = [f"# {library.item_title(item)}",
                f"Elem: {item.id} · Csatorna: {meta.get('channel') or '-'} · "
                f"Hossz: {library.hms(meta.get('duration_s'))}"]
        if item.source.url:
            head.append(f"URL: {item.source.url}")
        head.append(f"Megnyitás: {_link(item.id)}")
        head.append(f"Átirat: {t.id}, {library.describe_transcript(t)}, nyelv: {t.language or '?'}"
                    + (", fordítás: magyar" if hu is not None else ""))
        others = [x for x in transcripts if x.id != t.id]
        if others:
            head.append("Többi átirat: " + "; ".join(
                f"{x.id} ({library.describe_transcript(x)}{', fő' if x.id == item.main_transcript_id else ''})"
                for x in others))
        for w in t.warnings or []:
            head.append(f"Figyelmeztetés: {w}")
        paragraphs = library.with_translation(library.turns(t.segments), hu)
        names = personal.names_for(s, item.id, t.id)
        for p in paragraphs:
            p["speaker"] = personal.rename(p["speaker"], names)
        tags = personal.tags_of(s, item.id)
        if tags:
            head.append("Címkék: " + ", ".join(tags))
        if item.note:
            head.append("Jegyzet: " + item.note[:2000])
    lines = ["\n".join(head), ""]
    size, shown, rest_from = len(lines[0]), 0, None
    for p in paragraphs:
        if p["end"] < tol_mp or (ig_mp is not None and p["start"] > ig_mp):
            continue
        who = f" {p['speaker']}:" if p["speaker"] else ""
        if nyelv == "magyar":
            body = p["hu"] or "—"
        elif nyelv == "mindketto":
            body = f"{p['text']}\n  [magyarul] {p['hu'] or '—'}"
        else:
            body = p["text"]
        line = f"[{library.hms(p['start'])}]{who} {body}"
        if size + len(line) > limit and shown:
            rest_from = int(p["start"])
            break
        lines.append(line)
        size += len(line) + 1
        shown += 1
    if not shown:
        lines.append("(Ebben a szakaszban nincs szöveg.)")
    if rest_from is not None:
        lines.append(f"\n[Folytatás: atirat(elem_id={elem_id}, tol_mp={rest_from}"
                     + (f", ig_mp={ig_mp:g}" if ig_mp is not None else "")
                     + (f", nyelv=\"{nyelv}\"" if nyelv != "eredeti" else "") + ")]")
    return "\n".join(lines)


def osszefoglalo(elem_id: int) -> str:
    """Egy elem aktuális magyar összefoglalója (Markdown, időbélyegekkel)."""
    caller = _caller()
    with db.user_session(caller.user_id) as s:
        item = _visible_item(s, elem_id)
        title = library.item_title(item)
        summary = s.get(Summary, item.current_summary_id) if item.current_summary_id else None
        if summary is None:
            pending = s.scalar(select(func.count()).select_from(Job).where(
                Job.item_id == item.id, Job.kind == "summarize", Job.state.in_(ACTIVE)))
            if pending:
                return f"Az összefoglaló készül ({title}); nézd meg később."
            return f"Ehhez az elemhez ({title}) nincs összefoglaló. Az átirat az `atirat` eszközzel olvasható."
        made = summary.created_at.date().isoformat() if summary.created_at else "?"
        return (f"# {title}\n(Összefoglaló, {summary.model_used or summary.model or summary.role}, {made})\n\n"
                + personal.rename_text(summary.text, personal.names_for(s, item.id, summary.transcript_id)))


def legutobbiak(darab: int = 10, csatorna: str | None = None, csak_osszefoglaloval: bool = False,
                cimke: str | None = None) -> dict[str, Any]:
    """A könyvtárba legutóbb került elemek, a legújabb elöl.

    darab: 1–50. csatorna: a csatornanév része (kis- és nagybetű mindegy).
    csak_osszefoglaloval: csak azok, amelyekhez már van összefoglaló. cimke: csak az ilyen címkéjűek."""
    from .models import Source
    caller = _caller()
    q = (select(Item, Transcript.language).join(Source, Source.id == Item.source_id)
         .outerjoin(Transcript, Transcript.id == Item.main_transcript_id)
         .order_by(Item.created_at.desc(), Item.id.desc()).limit(max(1, min(darab, 50))))
    if csatorna and csatorna.strip():
        like = "%" + csatorna.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        q = q.where(Source.meta["channel"].astext.ilike(like))
    if csak_osszefoglaloval:
        q = q.where(Item.current_summary_id.is_not(None))
    if cimke and cimke.strip():
        from .models import ItemTag
        q = q.where(Item.id.in_(select(ItemTag.item_id).where(func.lower(ItemTag.tag) == cimke.strip().lower())))
    if not caller.include_private:
        q = q.where(Item.is_public.is_(True))
    with db.user_session(caller.user_id) as s:
        out = []
        rows = s.execute(q).all()
        tags = personal.tags_by_item(s, [item.id for item, _language in rows])
        for item, language in rows:
            entry = _entry(item, language)
            entry["cimkek"] = tags.get(item.id, [])
            entry["hozzaadva"] = item.created_at.date().isoformat() if item.created_at else None
            entry["van_osszefoglalo"] = item.current_summary_id is not None
            out.append(entry)
    return {"elemek": out}


# ---- starting tools (tokens with can_start) ------------------------------------------------

def atiras_inditasa(url: str | None = None, elem_id: int | None = None,
                    mod: Literal["captions", "local", "cloud"] = "captions", modell: str | None = None,
                    nyelv: str | None = None, beszelok_szama: int | None = None,
                    beszelok_min: int | None = None, beszelok_max: int | None = None,
                    beszelok_nelkul: bool = False, osszefoglalo: bool = True,
                    fo_atirat: bool = False) -> dict[str, Any]:
    """Átírás indítása egy videó URL-jére (új elem), vagy egy meglévő elem újraírása más móddal.
    A munkát a felhasználó saját futtatója végzi.

    mod: captions (YouTube saját felirata: gyors, ingyenes, beszélők nélkül; alapértelmezés) | local (helyi
    Whisper a felhasználó GPU-ján, beszélőkkel) | cloud (felhős AssemblyAI; pénzbe kerül, CSAK ha a felhasználó
    kifejezetten kéri). modell: local: large-v3 | large-v3-turbo; cloud: auto | universal-2 | universal-3-5-pro.
    nyelv: kétbetűs kód (hu, en…), üresen automatikus. beszelok_szama: ha pontosan tudott; vagy
    beszelok_min / beszelok_max: legalább / legfeljebb ennyi; beszelok_nelkul: beszélők szétválasztása nélkül.
    fo_atirat: újraírásnál az új átirat legyen a fő átirat. Visszaad egy elem_id-t (és ha munka indult, a
    munka_id-t): a munka_allapota eszközzel követhető."""
    caller = _caller()
    if not caller.can_start:
        raise ToolFailure("Ez a token nem indíthat munkát.")
    if bool(url) == bool(elem_id):
        raise ToolFailure("Vagy url-t, vagy elem_id-t adj meg (egyszerre csak az egyiket).")
    speakers = ("none" if beszelok_nelkul else "exact" if beszelok_szama
                else "range" if (beszelok_min or beszelok_max) else "auto")
    form = {"mode": mod, "language": nyelv or "", "speakers": speakers, "num_speakers": str(beszelok_szama or ""),
            "min_speakers": str(beszelok_min or ""), "max_speakers": str(beszelok_max or ""),
            "model_local": modell or "", "model_cloud": modell or ""}
    options, errors = submit_mod.parse_options(form)
    if errors:
        raise ToolFailure(" ".join(errors))
    if elem_id:
        with db.user_session(caller.user_id) as s:
            _visible_item(s, elem_id)
        error = submit_mod.retranscribe(caller.user_id, elem_id, options, fo_atirat, osszefoglalo)
        if error:
            raise ToolFailure(error)
        item_id = elem_id
    else:
        try:
            ref = sources.parse(url or "")
        except sources.BadUrl as exc:
            raise ToolFailure(str(exc)) from None
        if options["mode"] == "captions" and ref.kind != "youtube":
            raise ToolFailure("YouTube-feliratot csak YouTube-videóhoz lehet kérni; válaszd a local vagy a cloud "
                              "módot.")
        from . import ownfiles
        try:
            ownfiles.check_storage(get_settings(), caller.user_id)
        except ownfiles.QuotaError as exc:
            raise ToolFailure(str(exc)) from None
        item_id = submit_mod.submit_url(caller.user_id, ref, options, osszefoglalo)
    with db.user_session(caller.user_id) as s:
        job = s.scalar(select(Job).where(Job.item_id == item_id, Job.kind == "transcribe")
                       .order_by(Job.id.desc()).limit(1))
        out: dict[str, Any] = {"elem_id": item_id, "link": _link(item_id)}
        if job is not None and job.state in ACTIVE:
            out.update(munka_id=job.id, allapot=jobs.STATE_LABELS.get(job.state, job.state))
        else:
            out["allapot"] = "Kész (egy korábbi, azonos beállítású átirat átvéve)."
    return out


def munka_allapota(munka_id: int) -> dict[str, Any]:
    """Egy munka állapota (sorban, fut, kész, hiba), a lépés, és ha van, az elem azonosítója és linkje."""
    caller = _caller()
    if not caller.can_start:
        raise ToolFailure("Ez a token nem kérdezhet munkát.")
    with db.user_session(caller.user_id) as s:
        job = s.get(Job, munka_id)
        item = s.get(Item, job.item_id) if job is not None and job.item_id else None
        if job is None or (item is not None and not item.is_public and not caller.include_private):
            raise ToolFailure(f"Nincs {munka_id} azonosítójú munka.")
        out: dict[str, Any] = {"munka_id": job.id, "fajta": jobs.KIND_LABELS.get(job.kind, job.kind),
                               "cim": job.title, "allapot": jobs.STATE_LABELS.get(job.state, job.state),
                               "lepes": jobs.stage_label(job.stage)}
        if (job.progress or {}).get("parts"):
            out["resz"] = f"{job.progress.get('part')}/{job.progress.get('parts')}"
        if job.error:
            out["uzenet" if job.state in ACTIVE else "hiba"] = job.error
        if job.item_id:
            out["elem_id"] = job.item_id
            out["link"] = _link(job.item_id)
            pending = s.scalars(select(Job.kind).where(Job.item_id == job.item_id, Job.state.in_(ACTIVE),
                                                       Job.kind != "ask")).all()
            if pending:
                out["folyamatban"] = sorted({jobs.KIND_LABELS.get(k, k) for k in pending})
    return out


# ---- tokens -------------------------------------------------------------------------------

def create_token(user_id: int, name: str, can_start: bool, include_private: bool) -> str:
    """Returns the token itself (shown once); only its hash is stored."""
    token = TOKEN_PREFIX + security.new_token(32)
    with db.user_session(user_id) as s:
        s.add(McpToken(user_id=user_id, name=(name.strip() or "MCP-kliens")[:100],
                       token_hash=security.token_hash(token), can_start=can_start, include_private=include_private))
    return token


def revoke_token(user_id: int, token_id: int) -> None:
    with db.user_session(user_id) as s:
        s.execute(update(McpToken).where(McpToken.id == token_id, McpToken.revoked_at.is_(None))
                  .values(revoked_at=utcnow()))


def tokens(user_id: int) -> list[McpToken]:
    with db.user_session(user_id) as s:
        return list(s.scalars(select(McpToken).where(McpToken.revoked_at.is_(None)).order_by(McpToken.id)))


def caller_for(token: str) -> Caller | None:
    if not token.startswith(TOKEN_PREFIX) or len(token) > 200:
        return None
    from .models import User
    with db.system_session() as s:
        row = s.scalar(select(McpToken).where(McpToken.token_hash == security.token_hash(token),
                                              McpToken.revoked_at.is_(None)))
        if row is None:
            return None
        user = s.get(User, row.user_id)
        if user is None or user.disabled:
            return None
        if row.last_used_at is None or (utcnow() - row.last_used_at).total_seconds() > 60:
            row.last_used_at = utcnow()
        return Caller(row.user_id, row.id, row.can_start, row.include_private)


# ---- the servers and the HTTP endpoint -----------------------------------------------------

READING = (kereses, atirat, osszefoglalo, legutobbiak)
STARTING = (atiras_inditasa, munka_allapota)


def _wrap(fn):
    """Turn ToolFailure into the SDK's ToolError (an error result the model can read)."""
    from mcp.server.mcpserver.exceptions import ToolError

    @functools.wraps(fn)
    def run(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except ToolFailure as exc:
            raise ToolError(str(exc)) from None
    return run


def build_server(with_start: bool):
    from mcp.server.mcpserver import MCPServer
    from mcp.types import ToolAnnotations

    server = MCPServer(name="dwyt", title="dwyt könyvtár", instructions=INSTRUCTIONS, version="4")
    for fn in READING:
        server.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))(_wrap(fn))
    if with_start:
        server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False,
                                                idempotentHint=False, openWorldHint=True))(_wrap(atiras_inditasa))
        server.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))(_wrap(munka_allapota))
    return server


class McpEndpoint:
    """ASGI app at /mcp: token check and rate limit, then the SDK's handler for the token's tools."""

    def __init__(self) -> None:
        self.handlers: dict[bool, Any] = {}  # can_start -> handler, set while the app's lifespan runs

    async def __call__(self, scope, receive, send) -> None:
        from starlette.concurrency import run_in_threadpool
        from starlette.responses import JSONResponse, PlainTextResponse
        if not self.handlers:
            await PlainTextResponse("Not Found", status_code=404)(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        auth = headers.get(b"authorization", b"").decode("latin-1")
        given = auth[7:].strip() if auth[:7].lower() == "bearer " else ""
        caller = await run_in_threadpool(caller_for, given) if given else None
        client = (scope.get("client") or ("?",))[0]
        if caller is None:
            wait = ratelimit.hit("mcp-bad-token", client)
            status = 429 if wait else 401
            log.warning("mcp: bad token from %s", client)
            await JSONResponse({"error": "invalid_token"}, status_code=status,
                               headers={"WWW-Authenticate": 'Bearer realm="dwyt"'})(scope, receive, send)
            return
        if ratelimit.hit("mcp", str(caller.token_id)):
            await JSONResponse({"error": "rate_limited"}, status_code=429)(scope, receive, send)
            return
        reset = CALLER.set(caller)
        try:
            await self.handlers[caller.can_start](scope, receive, send)
        finally:
            CALLER.reset(reset)


endpoint = McpEndpoint()


@contextlib.asynccontextmanager
async def lifespan():
    """Two MCP servers (reading only; reading and starting), each with its session manager."""
    from mcp.server.streamable_http_manager import StreamableHTTPASGIApp
    from mcp.server.transport_security import TransportSecuritySettings

    logging.getLogger("mcp").setLevel(logging.WARNING)  # one INFO line per stateless request otherwise
    async with contextlib.AsyncExitStack() as stack:
        handlers = {}
        for with_start in (False, True):
            server = build_server(with_start)
            # the token guards the endpoint; the Host check would only refuse proxied requests
            server.streamable_http_app(stateless_http=True, json_response=True,
                                       transport_security=TransportSecuritySettings(
                                           enable_dns_rebinding_protection=False))
            manager = server.session_manager
            await stack.enter_async_context(manager.run())
            handlers[with_start] = StreamableHTTPASGIApp(manager)
        endpoint.handlers = handlers
        try:
            yield
        finally:
            endpoint.handlers = {}
