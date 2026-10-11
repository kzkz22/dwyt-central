"""Safe HTML from the small Markdown subset the summary prompt asks for (ported from yt-transcribe-webui).

Only headings (##), bullet lists (-, *), paragraphs and **bold** are recognised; everything is
escaped first, so model output can never inject markup. Timestamps like [12:34] or [01:02:03]
become links: to the video at that second (YouTube) or to that line of the transcript.
"""
from __future__ import annotations

import re
from typing import Callable

from markupsafe import Markup, escape

_TS = re.compile(r"\[(?:(\d{1,2}):)?([0-5]?\d):([0-5]\d)\]")
_BOLD = re.compile(r"\*\*(.+?)\*\*")


def seconds_of(match: re.Match) -> int:
    h, m, s = match.group(1), match.group(2), match.group(3)
    return int(h or 0) * 3600 + int(m) * 60 + int(s)


def _inline(text: str, link: Callable[[int], tuple[str, bool]]) -> str:
    out = str(escape(text))
    out = _BOLD.sub(r"<strong>\1</strong>", out)

    def ts(m: re.Match) -> str:
        href, external = link(seconds_of(m))
        attrs = ' target="_blank" rel="noreferrer noopener"' if external else ""
        return f'<a class="ts" href="{escape(href)}"{attrs}>{m.group(0)[1:-1]}</a>'
    return _TS.sub(ts, out)


def summary_html(text: str, link: Callable[[int], tuple[str, bool]]) -> Markup:
    """link(seconds) -> (href, opens_elsewhere)."""
    html: list[str] = []
    para: list[str] = []
    in_list = False

    def flush_para() -> None:
        if para:
            html.append("<p>" + " ".join(para) + "</p>")
            para.clear()

    for raw in text.replace("\r\n", "\n").split("\n"):
        line = raw.strip()
        bullet = re.match(r"^[-*•]\s+(.*)$", line)
        heading = re.match(r"^#{1,6}\s+(.*)$", line)
        if bullet:
            flush_para()
            if not in_list:
                html.append("<ul>")
                in_list = True
            html.append(f"<li>{_inline(bullet.group(1), link)}</li>")
            continue
        if in_list:
            html.append("</ul>")
            in_list = False
        if not line:
            flush_para()
        elif heading:
            flush_para()
            if not html and heading.group(1).strip("# ").lower() == "összefoglaló":
                continue  # the page already titles the block
            html.append(f"<h3>{_inline(heading.group(1).strip('# '), link)}</h3>")
        else:
            para.append(_inline(line, link))
    flush_para()
    if in_list:
        html.append("</ul>")
    return Markup("\n".join(html))


def plain_preview(text: str, limit: int = 220) -> str:
    """First sentences of the summary section, for the library list."""
    body = []
    for line in text.split("\n"):
        line = line.strip()
        if line.startswith("#"):
            if body:
                break
            continue
        if line.startswith(("-", "*", "•")):
            if body:
                break
            continue
        if line:
            body.append(_BOLD.sub(r"\1", line))
    joined = " ".join(body)
    return joined if len(joined) <= limit else joined[:limit].rsplit(" ", 1)[0] + " …"
