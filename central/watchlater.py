"""The Megnézendő list (round 8, decision 19.1): videos the user wants to watch later, kept in his own YouTube
account as the playlist "dwyt – megnézendő" (YouTube's built-in "Watch later" list cannot be written through
the API). dwyt remembers each video's playlist item, so removing it here removes it there; the daily sync
takes over what the user changed on YouTube (removed, or added to that playlist by hand).

Needs the write consent (youtube scope), asked the first time the user presses Megnézendő.
"""
from __future__ import annotations

import logging
import re

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert

from . import db, secretbox, youtube
from .config import Settings
from .models import Item, Source, WatchLater, YoutubeLink

log = logging.getLogger("dwyt.watchlater")
VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")


class NeedConsent(Exception):
    """The user has not allowed dwyt to manage his playlists yet."""


def can_write(user_id: int) -> bool:
    with db.system_session() as s:
        link = s.get(YoutubeLink, user_id)
        return bool(link is not None and link.can_write)


def video_ids(user_id: int) -> set[str]:
    with db.user_session(user_id) as s:
        return set(s.scalars(select(WatchLater.video_id)))


def video_of(item: Item) -> str | None:
    """The YouTube video id of a library item (None for other sources)."""
    key = item.source.key if item.source is not None else ""
    vid = key.removeprefix("youtube:") if key.startswith("youtube:") else ""
    return vid if VIDEO_ID.match(vid) else None


def _token_and_playlist(settings: Settings, user_id: int) -> tuple[str, str]:
    with db.system_session() as s:
        link = s.get(YoutubeLink, user_id)
        if link is None or not link.can_write:
            raise NeedConsent()
        refresh, playlist = secretbox.unseal(link.refresh_token_enc), link.watch_playlist_id
    token = youtube.access_token(settings, refresh)
    if not playlist:
        playlist = youtube.find_or_create_playlist(settings, token)
        with db.system_session() as s:
            s.execute(update(YoutubeLink).where(YoutubeLink.user_id == user_id).values(watch_playlist_id=playlist))
    return token, playlist


def _forget_playlist(user_id: int) -> None:
    with db.system_session() as s:
        s.execute(update(YoutubeLink).where(YoutubeLink.user_id == user_id).values(watch_playlist_id=None))


def add(settings: Settings, user_id: int, video_id: str, title: str | None = None) -> None:
    """Put a video on the list (and on the YouTube playlist). NeedConsent / YoutubeError say why not."""
    if not VIDEO_ID.match(video_id or ""):
        raise youtube.YoutubeError("Ez nem YouTube-videó.")
    if video_id in video_ids(user_id):
        return
    token, playlist = _token_and_playlist(settings, user_id)
    try:
        item_id, known = youtube.playlist_add(settings, token, playlist, video_id)
    except youtube.PlaylistGone:  # the playlist was deleted on YouTube: make a new one
        _forget_playlist(user_id)
        token, playlist = _token_and_playlist(settings, user_id)
        item_id, known = youtube.playlist_add(settings, token, playlist, video_id)
    with db.system_session() as s:
        title = (title or known or "")[:500] or None
        s.execute(insert(WatchLater).values(user_id=user_id, video_id=video_id, title=title,
                                            playlist_item_id=item_id)
                  .on_conflict_do_update(index_elements=[WatchLater.user_id, WatchLater.video_id],
                                         set_={"playlist_item_id": item_id}))


def remove(settings: Settings, user_id: int, video_id: str) -> None:
    with db.system_session() as s:
        row = s.scalar(select(WatchLater).where(WatchLater.user_id == user_id, WatchLater.video_id == video_id))
        item_id = row.playlist_item_id if row is not None else None
    if item_id:
        token, _playlist = _token_and_playlist(settings, user_id)
        youtube.playlist_remove(settings, token, item_id)
    with db.system_session() as s:
        s.execute(delete(WatchLater).where(WatchLater.user_id == user_id, WatchLater.video_id == video_id))


def sync(settings: Settings, user_id: int) -> dict[str, int]:
    """Take over the playlist as it is on YouTube: removed there = removed here, added there = added here."""
    if not can_write(user_id):
        return {"removed": 0, "added": 0}
    with db.system_session() as s:
        playlist = s.scalar(select(YoutubeLink.watch_playlist_id).where(YoutubeLink.user_id == user_id))
    if not playlist:
        return {"removed": 0, "added": 0}
    token, playlist = _token_and_playlist(settings, user_id)
    try:
        remote = youtube.playlist_items(settings, token, playlist)
    except youtube.PlaylistGone:  # deleted on YouTube: the list is empty now
        _forget_playlist(user_id)
        remote = []
    there = {vid: (pid, title) for pid, vid, title in remote}
    with db.system_session() as s:
        here = {r.video_id for r in s.scalars(select(WatchLater).where(WatchLater.user_id == user_id))}
        gone = here - set(there)
        if gone:
            s.execute(delete(WatchLater).where(WatchLater.user_id == user_id, WatchLater.video_id.in_(gone)))
        for vid in set(there) - here:
            pid, title = there[vid]
            s.execute(insert(WatchLater).values(user_id=user_id, video_id=vid, title=title, playlist_item_id=pid)
                      .on_conflict_do_nothing(index_elements=[WatchLater.user_id, WatchLater.video_id]))
    return {"removed": len(gone), "added": len(set(there) - here)}


def items_on_list(s, user_id: int):
    """A filter for the library query: items whose YouTube video is on the list (user session)."""
    keys = select("youtube:" + WatchLater.video_id).where(WatchLater.user_id == user_id)
    return Item.source_id.in_(select(Source.id).where(Source.key.in_(keys)))
