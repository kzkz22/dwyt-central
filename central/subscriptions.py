"""YouTube subscriptions (round 4a, ported from yt-transcribe-webui and made multi-user).

- Channels come from the user's YouTube account (Data API, with consent), a Takeout CSV or by hand.
- Feeds are polled centrally, once per channel for every follower (channels, channel_videos);
  each new video goes into the inbox of every active follower whose subscription is older
  than the video. Shorts are marked once per video.
- Per channel rule: always (processed at once), ask (inbox), never (not followed).
- The chain of a subscription video (a configured rule, every step shown on the job):
  YouTube captions -> no captions: local Whisper (waits for the user's GPU up to GPU_WAIT_H)
  -> cloud only if the channel allows it. Summary always; translation by the user's setting.

The work across users (polling, fan-out, the chain) runs in the system session, always with an
explicit user_id; pages use the user session (row-level security).
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session as DbSession
from sqlalchemy.orm import object_session

from . import db, indexing, jobs, reuse, secretbox, youtube
from .config import Settings, get_settings
from .db import utcnow
from .models import (ACTIVE, Channel, ChannelVideo, InboxEntry, Item, Job, Runner, Source, Subscription,
                     YoutubeLink)

log = logging.getLogger("dwyt.subscriptions")

RULES = {"always": "mindig", "ask": "kérdezzen", "never": "soha"}
SOURCES = {"google": "YouTube-fiók", "takeout": "Takeout", "manual": "kézi"}
NOTIFY = {"digest": "napi összesítő", "instant": "azonnali értesítés", "none": "nincs értesítés"}
MODE_LABELS = {"captions": "YouTube-felirat", "local": "helyi Whisper", "cloud": "felhős átírás"}
LATER_LIMIT_H = 48  # captions or download refused, live stream: how long a step keeps retrying
TICK_S = 20


# ---- channels and subscriptions -----------------------------------------------------------

def _ensure_channels(channels: list[tuple[str, str | None]]) -> None:
    with db.system_session() as s:
        for cid, title in channels:
            stmt = insert(Channel).values(id=cid, title=title)
            s.execute(stmt.on_conflict_do_update(index_elements=[Channel.id],
                                                 set_={"title": func.coalesce(stmt.excluded.title, Channel.title)}))


def add_subscriptions(user_id: int, channels: list[tuple[str, str | None]], source: str) -> int:
    """Follow these channels (rule: ask). A channel followed again only brings videos from now on.
    Returns how many are new."""
    channels = [(cid, title) for cid, title in channels if youtube.CHANNEL_ID.match(cid)]
    if not channels:
        return 0
    _ensure_channels(channels)
    added = 0
    with db.user_session(user_id) as s:
        for cid, _title in channels:
            sub = s.scalar(select(Subscription).where(Subscription.channel_id == cid))
            if sub is None:
                s.add(Subscription(user_id=user_id, channel_id=cid, source=source, baseline_at=utcnow()))
                added += 1
            elif not sub.active:
                sub.active, sub.baseline_at = True, utcnow()
                if source == "google":
                    sub.source = "google"
    return added


def remove_subscription(user_id: int, sub_id: int) -> None:
    """Stop following; the inbox entries not acted on go too, the library stays."""
    with db.user_session(user_id) as s:
        sub = s.get(Subscription, sub_id)
        if sub is None:
            return
        s.execute(text("DELETE FROM inbox_entries WHERE channel_id = :c AND state IN ('new', 'short', 'skipped')"),
                  {"c": sub.channel_id})
        s.delete(sub)


def update_subscription(user_id: int, sub_id: int, rule: str, allow_cloud: bool, notify: str) -> bool:
    if rule not in RULES or notify not in NOTIFY:
        return False
    with db.user_session(user_id) as s:
        sub = s.get(Subscription, sub_id)
        if sub is None:
            return False
        if sub.rule == "never" and rule != "never":
            sub.baseline_at = utcnow()  # not followed meanwhile: what came out since is not "new"
        sub.rule, sub.allow_cloud, sub.notify = rule, allow_cloud, notify
    return True


# ---- the YouTube account ------------------------------------------------------------------

def link_of(user_id: int) -> YoutubeLink | None:
    with db.system_session() as s:
        link = s.get(YoutubeLink, user_id)
        if link is not None:
            s.expunge(link)
        return link


def save_link(user_id: int, refresh_token: str, email: str | None) -> None:
    with db.system_session() as s:
        stmt = insert(YoutubeLink).values(user_id=user_id, email=email, refresh_token_enc=secretbox.seal(refresh_token))
        s.execute(stmt.on_conflict_do_update(index_elements=[YoutubeLink.user_id], set_={
            "email": stmt.excluded.email, "refresh_token_enc": stmt.excluded.refresh_token_enc,
            "created_at": func.now(), "synced_at": None, "sync_error": None}))


def unlink(settings: Settings, user_id: int) -> None:
    """Forget the consent (and revoke it at Google); the channels and their rules stay, as manual ones."""
    with db.system_session() as s:
        link = s.get(YoutubeLink, user_id)
        token = None
        if link is not None:
            try:
                token = secretbox.unseal(link.refresh_token_enc)
            except secretbox.NoKey:
                token = None
            s.delete(link)
    if token:
        youtube.revoke(settings, token)
    with db.user_session(user_id) as s:
        s.execute(update(Subscription).where(Subscription.source == "google").values(source="manual"))


def sync_google(settings: Settings, user_id: int) -> dict[str, Any]:
    """Read the subscription list of the connected account. Channels unsubscribed on YouTube are
    no longer followed (kept with their rules)."""
    link = link_of(user_id)
    if link is None:
        raise youtube.YoutubeError("Nincs összekapcsolt YouTube-fiók.")
    try:
        token = youtube.access_token(settings, secretbox.unseal(link.refresh_token_enc))
        subs = youtube.subscriptions(settings, token)
        if not subs:  # an empty answer is more likely a glitch than unsubscribing from everything
            raise youtube.YoutubeError("A YouTube üres feliratkozás-listát adott; a csatornák változatlanok.")
    except (youtube.YoutubeError, secretbox.NoKey) as exc:
        with db.system_session() as s:
            s.execute(update(YoutubeLink).where(YoutubeLink.user_id == user_id)
                      .values(synced_at=utcnow(), sync_error=str(exc)[:500]))
        raise
    added = add_subscriptions(user_id, subs, "google")
    ids = [cid for cid, _t in subs]
    with db.user_session(user_id) as s:
        gone = s.execute(update(Subscription).where(Subscription.source == "google", Subscription.active.is_(True),
                                                    Subscription.channel_id.not_in(ids))
                         .values(active=False)).rowcount
    with db.system_session() as s:
        s.execute(update(YoutubeLink).where(YoutubeLink.user_id == user_id)
                  .values(synced_at=utcnow(), sync_error=None, sync_count=len(subs)))
    return {"count": len(subs), "added": added, "gone": gone}


def sync_due(settings: Settings) -> None:
    before = utcnow() - timedelta(hours=settings.google_sync_hours)
    with db.system_session() as s:
        users = s.scalars(select(YoutubeLink.user_id).where(
            (YoutubeLink.synced_at.is_(None)) | (YoutubeLink.synced_at < before))).all()
    for user_id in users:
        try:
            sync_google(settings, user_id)
        except (youtube.YoutubeError, secretbox.NoKey) as exc:
            log.info("YouTube list of user %s not synced: %s", user_id, exc)


# ---- polling and fan-out ------------------------------------------------------------------

def poll_due(settings: Settings, limit: int = 30) -> int:
    """Check the feeds of followed channels not checked for FEED_POLL_MIN; returns new videos."""
    before = utcnow() - timedelta(minutes=settings.feed_poll_min)
    with db.system_session() as s:
        followed = select(Subscription.channel_id).where(Subscription.active.is_(True), Subscription.rule != "never")
        ids = s.scalars(select(Channel.id).where(
            Channel.id.in_(followed), (Channel.last_checked_at.is_(None)) | (Channel.last_checked_at < before))
            .order_by(Channel.last_checked_at.nulls_first()).limit(limit)).all()
    return sum(poll_channel(settings, cid) for cid in ids)


def poll_channel(settings: Settings, channel_id: str) -> int:
    try:
        videos, error = youtube.feed(settings, channel_id), None
    except youtube.YoutubeError as exc:
        videos, error = [], str(exc)
    new: list[str] = []
    with db.system_session() as s:
        ch = s.get(Channel, channel_id)
        if ch is None:
            return 0
        ch.last_checked_at, ch.last_error = utcnow(), error
        if videos and videos[0].channel_title:
            ch.title = videos[0].channel_title[:300]
        for v in videos:
            got = s.execute(insert(ChannelVideo).values(video_id=v.video_id, channel_id=channel_id, title=v.title,
                                                        published_at=v.published_at)
                            .on_conflict_do_nothing(index_elements=[ChannelVideo.video_id])
                            .returning(ChannelVideo.video_id)).scalar()
            if got:
                new.append(got)
    # the Shorts check is a web request per video: outside the transaction, once per video
    shorts = {vid: youtube.is_short(settings, vid) for vid in new}
    with db.system_session() as s:
        for vid, short in shorts.items():
            if short is not None:
                s.execute(update(ChannelVideo).where(ChannelVideo.video_id == vid).values(is_short=short))
    fan_out(settings, channel_id)
    return len(new)


def fan_out(settings: Settings, channel_id: str) -> None:
    """Put the channel's new videos into its followers' inboxes (and start the 'always' ones)."""
    with db.system_session() as s:
        subs = s.scalars(select(Subscription).where(Subscription.channel_id == channel_id,
                                                    Subscription.active.is_(True), Subscription.rule != "never")).all()
        if not subs:
            return
        oldest = min(sub.baseline_at for sub in subs)
        videos = s.scalars(select(ChannelVideo).where(
            ChannelVideo.channel_id == channel_id,
            func.coalesce(ChannelVideo.published_at, ChannelVideo.seen_at) > oldest)).all()
        for sub in subs:
            for v in videos:
                if (v.published_at or v.seen_at) <= sub.baseline_at:
                    continue
                entry_id = s.execute(insert(InboxEntry).values(
                    user_id=sub.user_id, channel_id=channel_id, video_id=v.video_id, title=v.title,
                    published_at=v.published_at, state="new")
                    .on_conflict_do_nothing(index_elements=[InboxEntry.user_id, InboxEntry.video_id])
                    .returning(InboxEntry.id)).scalar()
                if entry_id is None:
                    continue
                entry = s.get(InboxEntry, entry_id)
                if v.is_short:
                    entry.state = "short"
                elif _link_existing(s, entry):
                    pass
                elif sub.rule == "always":
                    request(s, settings, entry, sub)


# ---- the inbox ----------------------------------------------------------------------------

def _source(s: DbSession, video_id: str) -> Source:
    key = f"youtube:{video_id}"
    s.execute(insert(Source).values(kind="youtube", key=key, url=f"https://www.youtube.com/watch?v={video_id}",
                                    meta={}).on_conflict_do_nothing(index_elements=[Source.key]))
    return s.scalar(select(Source).where(Source.key == key))


def _link_existing(s: DbSession, entry: InboxEntry) -> bool:
    """A video already in the user's library (submitted by hand earlier): link it, do not redo it."""
    item_id = s.scalar(select(Item.id).join(Source, Source.id == Item.source_id)
                       .where(Item.user_id == entry.user_id, Source.key == f"youtube:{entry.video_id}"))
    if item_id is None:
        return False
    entry.state, entry.item_id, entry.updated_at = "done", item_id, utcnow()
    return True


def _whisper_model(s: DbSession, user_id: int) -> str:
    for runner in s.scalars(select(Runner).where(Runner.user_id == user_id, Runner.revoked_at.is_(None))
                            .order_by(Runner.last_seen_at.desc().nulls_last())):
        models = (runner.capabilities or {}).get("whisper_models") or []
        if models:
            return str(models[0])
    return "large-v3"


def request(s: DbSession, settings: Settings, entry: InboxEntry, sub: Subscription | None) -> None:
    """Start the chain for an inbox video (system session, the entry's user). Summary always."""
    if _link_existing(s, entry):
        return
    user_id = entry.user_id
    source = _source(s, entry.video_id)
    item = Item(user_id=user_id, source_id=source.id, is_public=True, title=None)
    s.add(item)
    s.flush()
    indexing.enqueue(s, item)
    options = {"mode": "captions", "language": None, "diarize": False}
    found = reuse.find(source, options)
    entry.item_id, entry.updated_at = item.id, utcnow()
    if found is not None:
        reuse.attach(s, user_id, item, found, True, reuse.translate_auto(user_id))
        entry.state = "done"
        return
    job = jobs.enqueue_transcription(s, user_id, item, source, options, summarize=True)
    job.title = entry.title or job.title
    chain = ["local"] + (["cloud"] if sub is not None and sub.allow_cloud else [])
    job.payload = {**job.payload, "origin": "subscription", "inbox_id": entry.id, "chain": chain,
                   "step_since": utcnow().isoformat()}
    entry.state, entry.job_id = "queued", job.id


def request_entry(settings: Settings, user_id: int, entry_id: int) -> bool:
    with db.system_session() as s:
        entry = s.get(InboxEntry, entry_id, with_for_update=True)
        if entry is None or entry.user_id != user_id or entry.state not in ("new", "skipped", "short", "failed"):
            return False
        sub = s.scalar(select(Subscription).where(Subscription.user_id == user_id,
                                                  Subscription.channel_id == entry.channel_id))
        request(s, settings, entry, sub)
        return True


def skip(user_id: int, entry_ids: list[int] | None) -> int:
    """Skip one entry, or (None) every new one."""
    with db.user_session(user_id) as s:
        query = update(InboxEntry).where(InboxEntry.state == "new")
        if entry_ids is not None:
            query = query.where(InboxEntry.id.in_(entry_ids))
        return s.execute(query.values(state="skipped", updated_at=utcnow())).rowcount


SYNC_SQL = """UPDATE inbox_entries e SET
    state = CASE j.state WHEN 'done' THEN 'done' WHEN 'failed' THEN 'failed' ELSE 'skipped' END,
    item_id = coalesce(e.item_id, j.item_id), updated_at = now()
  FROM jobs j WHERE e.job_id = j.id AND e.state = 'queued' AND j.state IN ('done', 'failed', 'cancelled')"""


def sync_inbox(s: DbSession) -> None:
    """Inbox entries follow their job's end (in a user session: that user's rows only)."""
    s.execute(text(SYNC_SQL))


# ---- the chain (called by jobs.fail for subscription jobs) ---------------------------------

def chain_step(job: Job, retry_mode: str, error: str, settings: Settings) -> bool:
    """Move a subscription job on in its chain, or let it wait, instead of failing. True if handled."""
    payload = dict(job.payload or {})
    if payload.get("origin") != "subscription":
        return False
    options = dict(payload.get("options") or {})
    mode = options.get("mode")
    chain = list(payload.get("chain") or [])
    now = utcnow()

    def requeue(delay_min: float, note: str) -> bool:
        job.payload = payload
        job.state, job.stage, job.run_after = "queued", "queued", now + timedelta(minutes=delay_min)
        job.attempts, job.finished_at = 0, None
        job.error = note[:2000]
        return True

    def advance(note: str) -> bool:
        if not chain:
            return False
        nxt = chain.pop(0)
        session = object_session(job)
        # local: the Whisper model the user's runners offer now (they may have changed since the request)
        model = _whisper_model(session, job.user_id) if nxt == "local" and session is not None else "auto"
        options.update(mode=nxt, diarize=True, model=model)
        payload.update(options=options, chain=chain, step_since=now.isoformat())
        return requeue(0, f"{note} Következik: {MODE_LABELS.get(nxt, nxt)}.")

    if retry_mode == "next_step":
        return advance(error)
    if retry_mode == "later":
        try:
            since = datetime.fromisoformat(payload.get("step_since") or "")
        except ValueError:
            since = job.created_at
        limit_h = settings.gpu_wait_h if mode == "local" else LATER_LIMIT_H
        if now - since < timedelta(hours=limit_h):
            return requeue(90 if mode == "local" else 60, error)
        return advance(f"{limit_h:g} óra alatt sem sikerült: {error}")
    return False


def gpu_waiting(s: DbSession) -> int:
    """The user's subscription videos waiting for local Whisper (user session)."""
    return s.scalar(select(func.count()).select_from(Job).where(
        Job.kind == "transcribe", Job.state.in_(ACTIVE), Job.payload["origin"].astext == "subscription",
        Job.payload["options"]["mode"].astext == "local")) or 0


# ---- background ---------------------------------------------------------------------------

class Background(threading.Thread):
    """Inside the web service: feeds, the daily YouTube list sync, inbox states."""

    def __init__(self) -> None:
        super().__init__(daemon=True, name="subscriptions")
        self.stop = threading.Event()
        self._last_sync = 0.0

    def tick(self) -> None:
        settings = get_settings()
        poll_due(settings)
        if time.monotonic() - self._last_sync > 300:
            self._last_sync = time.monotonic()
            sync_due(settings)
        with db.system_session() as s:
            sync_inbox(s)

    def run(self) -> None:
        log.info("subscriptions started")
        while not self.stop.is_set():
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - keep the thread alive
                log.exception("subscriptions error")
            self.stop.wait(TICK_S)
