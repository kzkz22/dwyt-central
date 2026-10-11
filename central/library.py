"""Display helpers for the library: times, paragraphs of one speaker, transcript labels."""
from __future__ import annotations

from typing import Any

MODE_LABELS = {"captions": "YouTube-felirat", "cloud": "Felhős átírás", "local": "Helyi Whisper"}


def hms(seconds: float | None) -> str:
    s = int(max(seconds or 0, 0))
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60:02d}:{s % 60:02d}"


def turns(segments: list[Any], max_chars: int = 900, max_gap: float = 20.0) -> list[dict]:
    """Consecutive segments of one speaker as paragraphs."""
    out: list[dict] = []
    for seg in segments:
        cur = out[-1] if out else None
        if (cur is not None and cur["speaker"] == seg.speaker
                and len(cur["text"]) + 1 + len(seg.text) <= max_chars and seg.start_s - cur["end"] <= max_gap):
            cur["text"] += " " + seg.text
            cur["end"] = seg.end_s
            cur["idx"].append(seg.idx)
        else:
            out.append({"start": seg.start_s, "end": seg.end_s, "speaker": seg.speaker, "text": seg.text,
                        "idx": [seg.idx]})
    return out


def with_translation(paragraphs: list[dict], hu: dict[int, str] | None) -> list[dict]:
    """Adds the Hungarian text of each paragraph (the translated lines of its segments)."""
    for p in paragraphs:
        p["hu"] = " ".join(hu[i] for i in p["idx"] if hu.get(i)) if hu else ""
    return paragraphs


def describe_transcript(t: Any) -> str:
    if t.mode == "captions":
        return "YouTube saját felirat" if t.caption_kind == "manual" else "YouTube automatikus felirat"
    if t.mode == "cloud":
        return f"Felhős átírás ({t.model})"
    return f"Helyi Whisper ({t.model})"


def timestamp_url(source: Any, seconds: float) -> str | None:
    if source.kind == "youtube" and source.key.startswith("youtube:"):
        return f"https://www.youtube.com/watch?v={source.key.split(':', 1)[1]}&t={int(seconds)}s"
    return None


def item_title(item: Any) -> str:
    meta = item.source.meta or {}
    return item.title or meta.get("title") or item.source.url or item.source.key


# ---- export -------------------------------------------------------------------------------

def _srt_time(seconds: float) -> str:
    ms = int(round(max(seconds, 0) * 1000))
    return f"{ms // 3600000:02d}:{ms % 3600000 // 60000:02d}:{ms % 60000 // 1000:02d},{ms % 1000:03d}"


def to_txt(item: Any, t: Any, hu: dict[int, str] | None = None) -> str:
    """The transcript as text, paragraph per speaker turn; with hu, the Hungarian translation in the
    same layout (lines without a translation keep the original)."""
    meta = item.source.meta or {}
    lines = [f"# {item_title(item)}"]
    if item.source.url:
        lines.append(f"URL: {item.source.url}")
    if meta.get("channel"):
        lines.append(f"Csatorna: {meta['channel']}")
    if meta.get("duration_s"):
        lines.append(f"Hossz: {hms(meta['duration_s'])}")
    language = f"hu (fordítás, eredeti: {t.language or '?'})" if hu is not None else (t.language or "?")
    lines += [f"Nyelv: {language}", f"Átirat: {describe_transcript(t)}"]
    if t.diarized and t.speakers:
        lines.append("Beszélők: " + ", ".join(str(sp.get("label")) for sp in t.speakers if sp.get("label")))
    for warning in t.warnings or []:
        lines.append(f"Figyelmeztetés: {warning}")
    lines.append("")
    for p in turns(t.segments):
        text = " ".join(hu.get(i) or "" for i in p["idx"]).strip() if hu is not None else p["text"]
        who = f" {p['speaker']}:" if p["speaker"] else ""
        lines += [f"[{hms(p['start'])}]{who} {text or p['text']}", ""]
    return "\n".join(lines).rstrip() + "\n"


def to_srt(t: Any, hu: dict[int, str] | None = None) -> str:
    cues = []
    for n, seg in enumerate(t.segments, 1):
        end = max(seg.end_s, seg.start_s + 0.3)
        who = f"[{seg.speaker}] " if seg.speaker else ""
        body = (hu.get(seg.idx) or seg.text) if hu is not None else seg.text
        cues.append(f"{n}\n{_srt_time(seg.start_s)} --> {_srt_time(end)}\n{who}{body}\n")
    return "\n".join(cues)


def summary_markdown(item: Any, summary: Any) -> str:
    meta = item.source.meta or {}
    head = [f"# {item_title(item)}"]
    if item.source.url:
        head.append(f"URL: {item.source.url}")
    if meta.get("channel"):
        head.append(f"Csatorna: {meta['channel']}")
    if meta.get("duration_s"):
        head.append(f"Hossz: {hms(meta['duration_s'])}")
    head.append(f"Összefoglaló: {summary.model_used or summary.model or summary.role}, {summary.created_at:%Y-%m-%d}")
    return "\n".join(head) + "\n\n" + summary.text.strip() + "\n"


def file_base(item: Any, fallback: str) -> str:
    """A file name stem from the title: letters, digits, space, - and _ only."""
    base = "".join(c if c.isalnum() or c in "-_ " else "_" for c in item_title(item))[:80].strip()
    return base or fallback
