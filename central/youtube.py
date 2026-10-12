"""YouTube and Google for the subscriptions (ported from yt-transcribe-webui): channel feeds
(RSS), the Shorts check, channel lookup, Takeout import, and the subscription list through the
YouTube Data API.

The list is read with the user's consent (scope youtube.readonly, asked only when the user
connects YouTube on the Channels page): the same Web OAuth client and redirect address as the
sign-in, with PKCE, offline access (a refresh token, stored encrypted: secretbox.py).
All addresses come from settings, so the tests point them at fakes.
"""
from __future__ import annotations

import base64
import csv
import hashlib
import io
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.parse import urlencode

import httpx
from sqlalchemy import delete
from sqlalchemy.orm import Session as DbSession

from .config import Settings
from .db import utcnow
from .models import LoginState
from .security import new_token, token_hash

SCOPE = "https://www.googleapis.com/auth/youtube.readonly"
NS = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015",
      "media": "http://search.yahoo.com/mrss/"}
CHANNEL_ID = re.compile(r"^UC[A-Za-z0-9_-]{22}$")
VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_COOKIES = {"SOCS": "CAI"}  # YouTube's EU consent page would answer every request otherwise
_UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) dwyt"}
STATE_MINUTES = 10


class YoutubeError(Exception):
    """A user-facing reason (Hungarian)."""


class FeedUnavailable(YoutubeError):
    """The feed did not answer this time. YouTube's feed endpoint returns 404 or 500 for every channel
    for hours at a time (since late 2025, mostly at night), so this says nothing about the channel."""


FEED_TRIES = 2
FEED_RETRY_S = 3.0


@dataclass
class FeedVideo:
    video_id: str
    title: str
    published_at: datetime | None
    channel_title: str | None


def _client(timeout: float = 20) -> httpx.Client:
    return httpx.Client(timeout=timeout, headers=_UA, cookies=_COOKIES)


def feed(settings: Settings, channel_id: str) -> list[FeedVideo]:
    """The channel's latest videos (about 15) from its public feed; no sign-in, no quota."""
    reason = ""
    for attempt in range(FEED_TRIES):
        if attempt:
            time.sleep(FEED_RETRY_S)
        try:
            with _client() as c:
                resp = c.get(f"{settings.youtube_url}/feeds/videos.xml", params={"channel_id": channel_id})
        except httpx.HTTPError as exc:
            reason = f"nem érhető el ({type(exc).__name__})"
            continue
        if resp.status_code == 200:
            break
        if resp.status_code == 404 or resp.status_code >= 500 or resp.status_code == 429:
            reason = f"HTTP {resp.status_code}"
            continue
        raise YoutubeError(f"a YouTube HTTP {resp.status_code} választ adott")
    else:
        raise FeedUnavailable(f"a YouTube most nem adja ki a hírcsatornát ({reason})")
    try:
        root = ET.fromstring(resp.content)
    except ET.ParseError as exc:
        raise YoutubeError("a hírcsatorna nem értelmezhető") from exc
    channel_title = root.findtext("a:title", namespaces=NS)
    out = []
    for entry in root.findall("a:entry", NS):
        vid = entry.findtext("yt:videoId", namespaces=NS) or ""
        if not VIDEO_ID.match(vid):
            continue
        published = entry.findtext("a:published", namespaces=NS)
        try:
            when = datetime.fromisoformat(published.replace("Z", "+00:00")) if published else None
        except ValueError:
            when = None
        out.append(FeedVideo(vid, (entry.findtext("a:title", namespaces=NS) or vid)[:500], when, channel_title))
    return out


def is_short(settings: Settings, video_id: str) -> bool | None:
    """True for a Short: /shorts/<id> answers 200 for Shorts and redirects to /watch for others."""
    try:
        with _client(timeout=10) as c:
            resp = c.head(f"{settings.youtube_url}/shorts/{video_id}", follow_redirects=False)
    except httpx.HTTPError:
        return None
    if resp.status_code == 200:
        return True
    if resp.status_code in (301, 302, 303, 307, 308) and "/watch" in resp.headers.get("location", ""):
        return False
    return None


def resolve_channel(settings: Settings, text: str) -> tuple[str, str | None]:
    """Channel id (and title when known) from an id, a channel URL, an @handle or a handle URL."""
    text = text.strip()[:300]
    m = re.search(r"(UC[A-Za-z0-9_-]{22})", text)
    if m and ("/channel/" in text or CHANNEL_ID.match(text)):
        return m.group(1), None
    handle = None
    m = re.search(r"youtube\.com/(@[^/?#\s]+)", text)
    if m:
        handle = m.group(1)
    elif text.startswith("@"):
        handle = text.split()[0]
    if handle and not re.fullmatch(r"@[\w.\-]{3,100}", handle):
        raise YoutubeError("Érvénytelen @név.")
    if not handle:
        raise YoutubeError("Adj meg egy csatorna-azonosítót (UC…), csatorna-URL-t vagy @nevet.")
    try:
        with _client() as c:
            resp = c.get(f"{settings.youtube_url}/{handle}", follow_redirects=True)
    except httpx.HTTPError as exc:
        raise YoutubeError(f"a csatorna oldala nem érhető el: {exc}") from exc
    if resp.status_code != 200:
        raise YoutubeError(f"nincs ilyen csatorna ({handle})")
    m = re.search(r'"(?:externalId|channelId)":"(UC[A-Za-z0-9_-]{22})"', resp.text) or \
        re.search(r'<link rel="canonical" href="[^"]*/channel/(UC[A-Za-z0-9_-]{22})"', resp.text)
    if not m:
        raise YoutubeError(f"a(z) {handle} csatorna azonosítója nem található az oldalon")
    title = re.search(r'<meta property="og:title" content="([^"]*)"', resp.text)
    return m.group(1), (title.group(1)[:300] if title else None)


def parse_takeout(data: bytes) -> list[tuple[str, str | None]]:
    """subscriptions.csv of Google Takeout (YouTube): Channel Id, Channel Url, Channel Title."""
    text = data.decode("utf-8-sig", errors="replace")
    out = []
    for row in csv.reader(io.StringIO(text)):
        if not row or not CHANNEL_ID.match(row[0].strip()):
            continue
        out.append((row[0].strip(), ((row[2].strip() if len(row) > 2 else None) or None)))
    return out[:5000]


# ---- connecting YouTube (Web OAuth, offline access) ----------------------------------------

def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def connect_start(s: DbSession, settings: Settings, user_id: int, email: str) -> tuple[str, str]:
    """Remember the state for this user; return Google's consent URL and the state."""
    if not settings.google_client_id:
        raise YoutubeError("A Google-belépés nincs beállítva (GOOGLE_CLIENT_ID).")
    state, nonce, verifier = new_token(), new_token(), new_token(48)
    s.execute(delete(LoginState).where(LoginState.created_at < utcnow() - timedelta(minutes=STATE_MINUTES)))
    s.add(LoginState(state_hash=token_hash(state), nonce=nonce, verifier=verifier, purpose="youtube",
                     user_id=user_id))
    params = {"client_id": settings.google_client_id, "redirect_uri": settings.redirect_uri,
              "response_type": "code", "scope": f"openid email {SCOPE}", "state": state, "nonce": nonce,
              "code_challenge": _b64url(hashlib.sha256(verifier.encode()).digest()),
              "code_challenge_method": "S256", "access_type": "offline", "prompt": "consent",
              "include_granted_scopes": "true", "login_hint": email}
    return f"{settings.google_auth_url}?{urlencode(params)}", state


def connect_finish(login: LoginState, settings: Settings, code: str) -> tuple[str, str | None]:
    """Exchange the code; returns the refresh token and the Google account's address."""
    from .accounts import _claims  # the ID token arrives over TLS from Google's token endpoint

    if (utcnow() - login.created_at) > timedelta(minutes=STATE_MINUTES):
        raise YoutubeError("Az összekapcsolás lejárt. Kezdd újra.")
    try:
        resp = httpx.post(settings.google_token_url, timeout=15, data={
            "code": code, "client_id": settings.google_client_id, "client_secret": settings.google_client_secret,
            "redirect_uri": settings.redirect_uri, "grant_type": "authorization_code",
            "code_verifier": login.verifier})
    except httpx.HTTPError as exc:
        raise YoutubeError("A Google nem érhető el; próbáld újra.") from exc
    if resp.status_code != 200:
        raise YoutubeError("A Google nem fogadta el az összekapcsolást; kezdd újra.")
    data = resp.json()
    if SCOPE not in str(data.get("scope") or "").split():
        raise YoutubeError("A Google nem adta meg a feliratkozások olvasásának jogát (a jóváhagyásnál "
                           "be kell pipálni). Kezdd újra.")
    if not data.get("refresh_token"):
        raise YoutubeError("A Google nem adott tartós hozzáférést; kezdd újra.")
    claims = _claims(data.get("id_token") or "") if data.get("id_token") else {}
    if claims and claims.get("aud") != settings.google_client_id:
        raise YoutubeError("A Google válasza nem ehhez az alkalmazáshoz szól.")
    return str(data["refresh_token"]), (str(claims["email"])[:200] if claims.get("email") else None)


def access_token(settings: Settings, refresh_token: str) -> str:
    try:
        resp = httpx.post(settings.google_token_url, timeout=15, data={
            "client_id": settings.google_client_id, "client_secret": settings.google_client_secret,
            "refresh_token": refresh_token, "grant_type": "refresh_token"})
    except httpx.HTTPError as exc:
        raise YoutubeError(f"a Google nem érhető el: {exc}") from exc
    if resp.status_code != 200:
        raise YoutubeError("A YouTube-hozzáférés lejárt vagy visszavonták; kapcsold össze újra a Csatornák "
                           "oldalon.")
    return str(resp.json()["access_token"])


def revoke(settings: Settings, refresh_token: str) -> None:
    """Best effort: the stored token is deleted anyway."""
    try:
        httpx.post(settings.google_revoke_url, data={"token": refresh_token}, timeout=10)
    except httpx.HTTPError:
        pass


def my_channel(settings: Settings, token: str) -> str | None:
    """The title of the YouTube channel the consent belongs to (to show which one is connected)."""
    try:
        resp = httpx.get(f"{settings.google_api_url}/youtube/v3/channels", params={"part": "snippet", "mine": "true"},
                         headers={"Authorization": f"Bearer {token}"}, timeout=20)
    except httpx.HTTPError:
        return None
    if resp.status_code != 200:
        return None
    items = resp.json().get("items") or []
    return str(items[0].get("snippet", {}).get("title") or "")[:200] or None if items else None


def subscriptions(settings: Settings, token: str) -> tuple[list[tuple[str, str | None]], int | None]:
    """(channel id, title) of every channel the connected account is subscribed to, and the total
    YouTube reports. Alphabetical order: the default (relevance) order can skip items while paging."""
    out: list[tuple[str, str | None]] = []
    seen: set[str] = set()
    total: int | None = None
    page = None
    with httpx.Client(timeout=20) as c:
        for _ in range(400):  # 50 per page: up to 20 000 subscriptions
            params: dict[str, str | int] = {"part": "snippet", "mine": "true", "maxResults": 50,
                                            "order": "alphabetical"}
            if page:
                params["pageToken"] = page
            try:
                resp = c.get(f"{settings.google_api_url}/youtube/v3/subscriptions", params=params,
                             headers={"Authorization": f"Bearer {token}"})
            except httpx.HTTPError as exc:
                raise YoutubeError(f"a YouTube Data API nem érhető el: {exc}") from exc
            if resp.status_code != 200:
                raise YoutubeError(f"a YouTube Data API hibát adott (HTTP {resp.status_code}); engedélyezve van a "
                                   "projektben a YouTube Data API v3?")
            data = resp.json()
            if total is None and isinstance((data.get("pageInfo") or {}).get("totalResults"), int):
                total = data["pageInfo"]["totalResults"]
            for it in data.get("items", []):
                sn = it.get("snippet", {})
                cid = str((sn.get("resourceId") or {}).get("channelId") or "")
                if CHANNEL_ID.match(cid) and cid not in seen:
                    seen.add(cid)
                    out.append((cid, (sn.get("title") or None)))
            page = data.get("nextPageToken")
            if not page:
                break
    return out, total
