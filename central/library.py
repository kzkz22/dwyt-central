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
