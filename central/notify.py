"""Notifications (round 4b, after yt-transcribe-webui): each user picks a channel, ntfy or Home
Assistant (notify.<service> through its REST API); the secrets are stored encrypted (secretbox).

  done    an item is ready (after its summary, or the transcript when no summary was asked):
          instantly for hand-made work (if the user wants it) and for channels set to "instant";
          collected for the daily digest for the other subscription channels
  error   a job failed for good: instantly (if the user wants it)
  digest  once a day at the user's time: the collected items and the inbox videos waiting
  test    from the Settings page

Rows go into an outbox (notifications) and are sent by the background thread, with retries.
The addresses are given by users, so requests never go to loopback, link-local or private
addresses, except the networks the operator allows (NOTIFY_PRIVATE_NETS) and for admins.
"""
from __future__ import annotations

import ipaddress
import logging
import re
import socket
from datetime import datetime, timedelta
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session as DbSession

from . import db, render, secretbox
from .config import Settings, get_settings
from .db import utcnow
from .models import InboxEntry, Item, Job, Notification, NotifySettings, Subscription, Summary, User

log = logging.getLogger("dwyt.notify")
MAX_ATTEMPTS = 16  # with the backoff below: about two days
CHANNELS = {"none": "nincs", "ntfy": "ntfy (telefonos alkalmazás)", "ha": "Home Assistant"}
TOPIC = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
SERVICE = re.compile(r"^[a-z0-9_]{1,100}$")
TIME = re.compile(r"^([01][0-9]|2[0-3]):[0-5][0-9]$")


class NotifyError(Exception):
    """A user-facing reason (Hungarian)."""


# ---- address safety ------------------------------------------------------------------------

def _allowed_private(settings: Settings) -> list[ipaddress._BaseNetwork]:
    nets = []
    for part in settings.notify_private_nets.split(","):
        part = part.strip()
        if part:
            try:
                nets.append(ipaddress.ip_network(part, strict=False))
            except ValueError:
                log.warning("bad NOTIFY_PRIVATE_NETS entry: %s", part)
    return nets


def check_url(url: str, is_admin: bool, settings: Settings | None = None) -> str:
    """The URL without a trailing slash, if it may be called; NotifyError otherwise."""
    settings = settings or get_settings()
    url = (url or "").strip().rstrip("/")
    parts = urlsplit(url)
    if parts.scheme not in ("https", "http") or not parts.hostname or parts.username or parts.password:
        raise NotifyError("Adj meg egy http(s) címet, például https://ntfy.sh.")
    if len(url) > 300:
        raise NotifyError("Túl hosszú cím.")
    try:
        infos = socket.getaddrinfo(parts.hostname, parts.port or (443 if parts.scheme == "https" else 80),
                                   proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise NotifyError(f"A(z) {parts.hostname} gépnév nem oldható fel.") from exc
    allowed = _allowed_private(settings)
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_global:
            continue
        if is_admin or any(ip in net for net in allowed):
            continue
        raise NotifyError("Ez a cím belső hálózatra mutat; ilyet csak az üzemeltető engedélyezhet "
                          "(NOTIFY_PRIVATE_NETS).")
    if parts.scheme == "http" and not is_admin and all(ipaddress.ip_address(i[4][0]).is_global for i in infos):
        raise NotifyError("Interneten át csak https-cím adható meg (a token titkosítatlanul menne).")
    return url


# ---- settings -------------------------------------------------------------------------------

def get(user_id: int) -> NotifySettings:
    with db.system_session() as s:
        row = s.get(NotifySettings, user_id)
        if row is None:
            return NotifySettings(user_id=user_id, channel="none", digest_time="19:00", on_manual=True, on_error=True)
        s.expunge(row)
        return row


def save(user_id: int, is_admin: bool, form: dict[str, str]) -> NotifySettings:
    """Validate and store the user's channel; empty token fields keep the stored token."""
    channel = form.get("channel", "none")
    if channel not in CHANNELS:
        raise NotifyError("Ismeretlen értesítési csatorna.")
    digest_time = (form.get("digest_time") or "19:00").strip()
    if not TIME.match(digest_time):
        raise NotifyError("Az összesítő ideje ÓÓ:PP alakú legyen, például 19:00.")
    values: dict = {"channel": channel, "digest_time": digest_time, "on_manual": form.get("on_manual") == "on",
                    "on_error": form.get("on_error") == "on", "updated_at": utcnow()}
    if channel == "ntfy":
        topic = (form.get("ntfy_topic") or "").strip()
        if not TOPIC.match(topic):
            raise NotifyError("Az ntfy-téma csak betűből, számból, - és _ jelből álljon (legfeljebb 64). Válassz "
                              "nehezen kitalálhatót: aki ismeri, olvashatja az értesítéseidet.")
        values.update(ntfy_server=check_url(form.get("ntfy_server") or "https://ntfy.sh", is_admin),
                      ntfy_topic=topic)
        if form.get("ntfy_token"):
            values["ntfy_token_enc"] = secretbox.seal(form["ntfy_token"].strip())
        if form.get("ntfy_token_clear") == "on":
            values["ntfy_token_enc"] = None
    elif channel == "ha":
        service = (form.get("ha_service") or "").strip().removeprefix("notify.")
        if not SERVICE.match(service):
            raise NotifyError("A Home Assistant értesítési szolgáltatás neve például mobile_app_telefon.")
        values.update(ha_url=check_url(form.get("ha_url") or "", is_admin), ha_service=service)
        if form.get("ha_token"):
            values["ha_token_enc"] = secretbox.seal(form["ha_token"].strip())
        current = get(user_id)
        if not values.get("ha_token_enc") and not current.ha_token_enc:
            raise NotifyError("A Home Assistanthez hosszú lejáratú hozzáférési token kell (Profil → Biztonság).")
    with db.system_session() as s:
        row = s.get(NotifySettings, user_id)
        if row is None:
            row = NotifySettings(user_id=user_id)
            s.add(row)
        for key, value in values.items():
            setattr(row, key, value)
        s.flush()
        s.expunge(row)
        return row


# ---- recording ------------------------------------------------------------------------------

def _active_channel(s: DbSession, user_id: int) -> NotifySettings | None:
    row = s.get(NotifySettings, user_id)
    return row if row is not None and row.channel != "none" else None


def _url(path: str) -> str:
    return f"{get_settings().base_url}{path}"


def _two_sentences(text: str) -> str:
    preview = render.plain_preview(text, limit=400)
    return " ".join(re.split(r"(?<=[.!?])\s+", preview)[:2])


def _origin(s: DbSession, item: Item) -> tuple[str, str]:
    """('manual', _) or ('subscription', its notify setting)."""
    entry = s.scalar(select(InboxEntry).where(InboxEntry.user_id == item.user_id, InboxEntry.item_id == item.id)
                     .limit(1))
    if entry is None:
        return "manual", ""
    sub = s.scalar(select(Subscription).where(Subscription.user_id == item.user_id,
                                              Subscription.channel_id == entry.channel_id))
    return "subscription", (sub.notify if sub is not None else "digest")


def item_done(s: DbSession, item: Item) -> Notification | None:
    """Record an item's "ready" notification once (system session, the item's user)."""
    cfg = _active_channel(s, item.user_id)
    if cfg is None or item.notified_at is not None:
        return None
    origin, how = _origin(s, item)
    if (origin == "manual" and not cfg.on_manual) or how == "none":
        return None
    summary = s.get(Summary, item.current_summary_id) if item.current_summary_id else None
    title = item.title or (item.source.meta or {}).get("title") or "Átirat"
    note = Notification(user_id=item.user_id, kind="done", digest=how == "digest", item_id=item.id,
                        title=str(title)[:200], url=_url(f"/elem/{item.id}"),
                        message=(_two_sentences(summary.text) if summary else "") or "Az átirat elkészült.")
    s.add(note)
    item.notified_at = utcnow()
    return note


def job_failed(s: DbSession, job: Job) -> Notification | None:
    cfg = _active_channel(s, job.user_id)
    if cfg is None or not cfg.on_error:
        return None
    note = Notification(user_id=job.user_id, kind="error", item_id=job.item_id,
                        title=f"Hiba: {job.title or 'munka'}"[:200], message=(job.error or "ismeretlen hiba")[:500],
                        url=_url(f"/elem/{job.item_id}" if job.item_id else "/munkak"))
    s.add(note)
    return note


# ---- sending --------------------------------------------------------------------------------

def _post(cfg: NotifySettings, note: Notification, is_admin: bool) -> None:
    if cfg.channel == "ntfy":
        server = check_url(cfg.ntfy_server or "https://ntfy.sh", is_admin)
        headers = {}
        if cfg.ntfy_token_enc:
            headers["Authorization"] = f"Bearer {secretbox.unseal(cfg.ntfy_token_enc)}"
        body = {"topic": cfg.ntfy_topic, "title": note.title, "message": note.message, "tags": ["dwyt"]}
        if note.url:
            body["click"] = note.url
        resp = httpx.post(server, json=body, headers=headers, timeout=15)
    elif cfg.channel == "ha":
        base = check_url(cfg.ha_url or "", is_admin)
        data: dict = {"title": note.title, "message": note.message}
        if note.url:
            data["data"] = {"url": note.url, "clickAction": note.url}  # iOS / Android companion app
        resp = httpx.post(f"{base}/api/services/notify/{cfg.ha_service}", json=data, timeout=15,
                          headers={"Authorization": f"Bearer {secretbox.unseal(cfg.ha_token_enc or '')}"})
    else:
        raise NotifyError("Nincs értesítési csatorna beállítva.")
    if resp.status_code >= 300:
        raise NotifyError(f"A szolgáltatás HTTP {resp.status_code} választ adott: {resp.text[:200]}")


def _retry_due(note: Notification) -> bool:
    """1, 2, 4 ... minutes after the previous tries, at most 6 hours apart."""
    waited = sum(min(60 * 2 ** i, 6 * 3600) for i in range(note.attempts))
    return (utcnow() - note.created_at).total_seconds() >= waited


def send_one(note_id: int) -> str | None:
    """Send one outbox row; returns the error (None when sent)."""
    with db.system_session() as s:
        note = s.get(Notification, note_id, with_for_update=True)
        if note is None or note.sent_at is not None:
            return None
        cfg = s.get(NotifySettings, note.user_id)
        user = s.get(User, note.user_id)
        note.attempts += 1
        try:
            if cfg is None:
                raise NotifyError("Nincs értesítési csatorna beállítva.")
            _post(cfg, note, bool(user and user.role == "admin"))
            note.sent_at, note.error = utcnow(), None
            return None
        except (NotifyError, secretbox.NoKey, httpx.HTTPError) as exc:
            note.error = str(exc)[:300]
            log.info("notification %s not sent: %s", note_id, exc)
            return note.error


def send_pending() -> int:
    with db.system_session() as s:
        notes = s.scalars(select(Notification).where(Notification.sent_at.is_(None), Notification.digest.is_(False),
                                                     Notification.attempts < MAX_ATTEMPTS)
                          .order_by(Notification.id).limit(50)).all()
        due = [n.id for n in notes if _retry_due(n)]
    return sum(1 for nid in due if send_one(nid) is None)


def make_digests(settings: Settings, now: datetime | None = None) -> int:
    """For every user whose digest time has come today: one notification of the collected items
    and the waiting inbox. Returns how many digests were made."""
    now_local = (now or utcnow()).astimezone(ZoneInfo(settings.timezone))
    made = 0
    with db.system_session() as s:
        rows = s.scalars(select(NotifySettings).where(NotifySettings.channel != "none")).all()
        for cfg in rows:
            hour, minute = (int(x) for x in cfg.digest_time.split(":"))
            if (now_local.hour, now_local.minute) < (hour, minute) or cfg.last_digest_on == now_local.date():
                continue
            cfg.last_digest_on = now_local.date()
            collected = s.scalars(select(Notification).where(
                Notification.user_id == cfg.user_id, Notification.digest.is_(True),
                Notification.sent_at.is_(None)).order_by(Notification.id)).all()
            waiting = s.scalar(select(func.count()).select_from(InboxEntry).where(
                InboxEntry.user_id == cfg.user_id, InboxEntry.state == "new",
                InboxEntry.created_at > utcnow() - timedelta(days=7))) or 0
            if not collected and not waiting:
                continue
            lines = [f"• {n.title}" for n in collected[:15]]
            if len(collected) > 15:
                lines.append(f"… és még {len(collected) - 15}")
            if waiting:
                lines.append(f"{waiting} új videó vár döntésre a postaládában.")
            s.add(Notification(user_id=cfg.user_id, kind="digest",
                               title=f"Napi összesítő: {len(collected)} új összefoglaló",
                               message="\n".join(lines), url=_url("/postalada" if waiting else "/")))
            for n in collected:
                n.sent_at, n.error = utcnow(), "az összesítőben"
            made += 1
    return made


def test(user_id: int) -> str | None:
    """Send a test notification at once; returns the error (None when it went out)."""
    with db.system_session() as s:
        note = Notification(user_id=user_id, kind="test", title="dwyt próba",
                            message="Ha ezt látod, az értesítés működik.", url=_url("/"))
        s.add(note)
        s.flush()
        note_id = note.id
    return send_one(note_id)


def last(user_id: int) -> Notification | None:
    with db.user_session(user_id) as s:
        return s.scalar(select(Notification).order_by(Notification.id.desc()).limit(1))
