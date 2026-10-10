"""What a submitted URL is: a YouTube video (keyed by its id) or another web page (keyed by
the normalised URL). The key identifies the source in the shared result store."""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit, urlunsplit

YT_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be",
            "www.youtube-nocookie.com", "youtube-nocookie.com"}
_YT_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")


class BadUrl(ValueError):
    pass


@dataclass
class SourceRef:
    kind: str  # youtube | url
    key: str
    url: str
    video_id: str | None = None


def youtube_id(url: str) -> str | None:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if host not in YT_HOSTS:
        return None
    path = parts.path.rstrip("/")
    if host == "youtu.be":
        candidate = path.lstrip("/").split("/")[0]
    elif path == "/watch":
        candidate = (parse_qs(parts.query).get("v") or [""])[0]
    else:
        m = re.match(r"^/(?:shorts|live|embed|v)/([^/]+)$", path)
        candidate = m.group(1) if m else ""
    return candidate if _YT_ID.match(candidate or "") else None


def parse(url: str) -> SourceRef:
    url = (url or "").strip()
    if not url.lower().startswith(("http://", "https://")) or len(url) > 2000 or any(c.isspace() for c in url):
        raise BadUrl("Az URL http:// vagy https:// kezdetű, szóköz nélküli cím legyen.")
    parts = urlsplit(url)
    if not parts.hostname:
        raise BadUrl("Az URL-ből hiányzik a gépnév.")
    vid = youtube_id(url)
    if vid:
        return SourceRef("youtube", f"youtube:{vid}", f"https://www.youtube.com/watch?v={vid}", vid)
    if (parts.hostname or "").lower() in YT_HOSTS:
        raise BadUrl("Ez nem egyetlen YouTube-videó címe (lejátszási lista vagy csatorna nem küldhető be).")
    normal = urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path or "/", parts.query, ""))
    return SourceRef("url", f"url:{normal}", normal)


def host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def domain_allowed(url: str, allowed: list[str] | None) -> bool:
    """A runner's allow-list: exact host or any subdomain of an entry; "*" allows everything."""
    if not allowed:
        return True
    h = host(url)
    for d in allowed:
        d = str(d).lower().strip()
        if d == "*" or h == d or h.endswith("." + d):
            return True
    return False
