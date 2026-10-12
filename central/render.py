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
    """The start of a summary as one plain paragraph (library list, notifications, the question
    catalogue): headings left out, list items kept (" · " between them), at most `limit` characters."""
    parts: list[str] = []
    size = 0
    for line in text.split("\n"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        bullet = line.startswith(("- ", "* ", "• "))
        line = _BOLD.sub(r"\1", line[2:].strip() if bullet else line)
        joiner = bullet and parts and not parts[-1].endswith((".", "!", "?", ":"))
        parts.append(("· " if joiner else "") + line)
        size += len(line) + 3
        if size > limit:
            break
    joined = " ".join(parts)
    return joined if len(joined) <= limit else joined[:limit].rsplit(" ", 1)[0] + " …"


# ---- answers to questions (round 6): a wider subset ----------------------------------------------

_CITE = re.compile(r"\[#(\d{1,5})(?:[ ,;]+((?:\d{1,2}:)?[0-5]?\d:[0-5]\d))?\]")
_CODE = re.compile(r"`([^`\n]+)`")
_ITALIC = re.compile(r"(?<![\w*])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![\w*])|(?<!\w)_(?!\s)([^_\n]+?)(?<!\s)_(?!\w)")
_OL = re.compile(r"^(\d{1,3})[.)]\s+(.*)$")
_UL = re.compile(r"^[-*•]\s+(.*)$")
_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_TABLE_SEP = re.compile(r"^\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?$")


def _seconds(text: str) -> int:
    parts = [int(x) for x in text.split(":")]
    return parts[0] * 3600 + parts[1] * 60 + parts[2] if len(parts) == 3 else parts[0] * 60 + parts[1]


def _chat_inline(text: str, ts_link: Callable[[int], tuple[str, bool]] | None,
                 src_link: Callable[[int, int | None], tuple[str, bool] | None] | None) -> str:
    """Escape first; then code spans (left alone), bold, italic, citations and timestamps."""
    codes: list[str] = []

    def keep(m: re.Match) -> str:
        codes.append(f"<code>{m.group(1)}</code>")
        return f"\x00{len(codes) - 1}\x00"
    out = _CODE.sub(keep, str(escape(text)))
    out = _BOLD.sub(r"<strong>\1</strong>", out)
    out = _ITALIC.sub(lambda m: f"<em>{m.group(1) or m.group(2)}</em>", out)

    def anchor(href: str, external: bool, label: str, cls: str) -> str:
        attrs = ' target="_blank" rel="noreferrer noopener"' if external else ""
        return f'<a class="{cls}" href="{escape(href)}"{attrs}>{label}</a>'

    def cite(m: re.Match) -> str:
        n, ts = int(m.group(1)), m.group(2)
        found = src_link(n, _seconds(ts) if ts else None) if src_link else None
        label = f"#{n} {ts}" if ts else f"#{n}"
        return anchor(found[0], found[1], label, "cite") if found else m.group(0)
    out = _CITE.sub(cite, out)
    if ts_link is not None:
        out = _TS.sub(lambda m: anchor(*ts_link(seconds_of(m)), m.group(0)[1:-1], "ts"), out)
    return re.sub(r"\x00(\d+)\x00", lambda m: codes[int(m.group(1))], out)


def _cells(line: str) -> list[str]:
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|"):
        line = line[:-1]
    return [c.strip() for c in line.split("|")]


def chat_html(text: str, ts_link: Callable[[int], tuple[str, bool]] | None = None,
              src_link: Callable[[int, int | None], tuple[str, bool] | None] | None = None) -> Markup:
    """A model's answer: headings, bullet and numbered lists, paragraphs, quotes, code blocks,
    simple tables, **bold**, *italic*, `code`. Everything is escaped first; links are made only from
    timestamps [12:34] (ts_link) and citations [#2 12:34] (src_link), never from the text itself."""
    html: list[str] = []
    para: list[str] = []
    lines = text.replace("\r\n", "\n").split("\n")
    inline = lambda value: _chat_inline(value, ts_link, src_link)  # noqa: E731
    list_tag: str | None = None
    i = 0

    def flush() -> None:
        nonlocal list_tag
        if para:
            html.append("<p>" + "<br>".join(para) + "</p>")
            para.clear()
        if list_tag:
            html.append(f"</{list_tag}>")
            list_tag = None

    while i < len(lines):
        raw = lines[i]
        line = raw.strip()
        if line.startswith("```"):
            flush()
            code: list[str] = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code.append(lines[i])
                i += 1
            html.append("<pre><code>" + str(escape("\n".join(code))) + "</code></pre>")
            i += 1
            continue
        if line.startswith("|") and i + 1 < len(lines) and _TABLE_SEP.match(lines[i + 1].strip()):
            flush()
            head = _cells(line)
            rows = []
            i += 2
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append(_cells(lines[i]))
                i += 1
            html.append('<div class="table-wrap"><table><thead><tr>'
                        + "".join(f"<th>{inline(c)}</th>" for c in head) + "</tr></thead><tbody>"
                        + "".join("<tr>" + "".join(f"<td>{inline(c)}</td>" for c in r) + "</tr>" for r in rows)
                        + "</tbody></table></div>")
            continue
        ol, ul, heading = _OL.match(line), _UL.match(line), _HEADING.match(line)
        if ol or ul:
            tag = "ol" if ol else "ul"
            if para:
                html.append("<p>" + "<br>".join(para) + "</p>")
                para.clear()
            if list_tag != tag:
                if list_tag:
                    html.append(f"</{list_tag}>")
                html.append(f"<{tag}>")
                list_tag = tag
            html.append(f"<li>{inline(ol.group(2) if ol else ul.group(1))}</li>")
        elif not line:
            flush()
        elif heading:
            flush()
            level = 3 if len(heading.group(1)) <= 3 else 4
            html.append(f"<h{level}>{inline(heading.group(2).strip('# '))}</h{level}>")
        elif line.startswith(">"):
            flush()
            html.append(f"<blockquote>{inline(line.lstrip('> ').strip())}</blockquote>")
        else:
            if list_tag:
                html.append(f"</{list_tag}>")
                list_tag = None
            para.append(inline(line))
        i += 1
    flush()
    return Markup("\n".join(html))
