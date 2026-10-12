"""What the model names of the user's runners mean (round 6): the runner reports, per name, what
runs behind it (dwyt-airouter's LiteLLM describes it), and the pages show it on the Runners page
and next to every model picker. The runner's report is checked again here: known fields only."""
from __future__ import annotations

from typing import Any

ROLE_DEFAULTS = {"summarize": "Összefoglaló", "translate": "Fordítás", "ask": "Kérdések",
                 "private": "Saját felvételek"}


def _text(value: Any, limit: int) -> str | None:
    return " ".join(str(value).split())[:limit] if isinstance(value, str) and value.strip() else None


def _clean(block: Any) -> dict[str, Any]:
    if not isinstance(block, dict):
        return {}
    out: dict[str, Any] = {}
    for key, limit in (("description", 200), ("target", 160), ("target_label", 160), ("provider", 40),
                       ("name", 100)):
        if _text(block.get(key), limit):
            out[key] = _text(block.get(key), limit)
    if block.get("where") in ("local", "cloud"):
        out["where"] = block["where"]
    for key in ("context", "thinking_budget"):
        value = block.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and 0 < value < 100_000_000:
            out[key] = value
    for key in ("vision", "thinking"):
        if isinstance(block.get(key), bool):
            out[key] = block[key]
    return out


def sanitize(raw: Any) -> dict[str, dict[str, Any]]:
    """The runner's llm_model_info, reduced to the fields the pages show."""
    out: dict[str, dict[str, Any]] = {}
    if not isinstance(raw, dict):
        return out
    for name, block in list(raw.items())[:40]:
        if not isinstance(name, str) or not name or len(name) > 100:
            continue
        info = _clean(block)
        fallback = block.get("fallback") if isinstance(block, dict) else None
        if isinstance(fallback, list):
            info["fallback"] = [_clean(f) for f in fallback[:3] if isinstance(f, dict)]
        out[name] = info
    return out


def merged(runners: list[Any]) -> dict[str, dict[str, Any]]:
    """Name -> info over the user's runners (the first runner that describes a name wins)."""
    out: dict[str, dict[str, Any]] = {}
    for r in runners:
        for name, info in ((r.capabilities or {}).get("llm_model_info") or {}).items():
            out.setdefault(name, info)
    return out


def defaults(runners: list[Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for r in runners:
        for task, name in ((r.capabilities or {}).get("llm_defaults") or {}).items():
            if isinstance(name, str):
                out.setdefault(task, name)
    return out


def thinking_text(info: dict[str, Any]) -> str:
    if info.get("thinking") is True:
        return "gondolkodással"
    if info.get("thinking") is False:
        return "gondolkodás nélkül"
    return ""


def where_text(info: dict[str, Any]) -> str:
    if info.get("where") == "local":
        return "a saját GPU-n"
    if info.get("where") == "cloud":
        return f"felhőben ({info['provider']})" if info.get("provider") else "felhőben"
    return ""


def offline_text(info: dict[str, Any]) -> str:
    """What happens when the GPU host is off."""
    if info.get("where") == "cloud":
        return "mindig felhőben fut"
    if info.get("where") != "local":
        return ""
    fallback = info.get("fallback") or []
    if not fallback:
        return "megvárja a GPU-gépet, felhőbe nem kerül"
    f = fallback[0]
    parts = [f.get("target_label") or f.get("name") or "felhős modell", thinking_text(f)]
    return "felhőben válaszol: " + ", ".join(p for p in parts if p)


def short(name: str, info: dict[str, Any] | None) -> str:
    """The option text of a model picker: "kerdes – Qwen3.6-35B-A3B, általános beállítás, gondolkodással"."""
    if not info:
        return name
    label = info.get("target_label") or ""
    parts = [p for p in (label, thinking_text(info)) if p]
    return f"{name} – {', '.join(parts)}" if parts else name


def hint(name: str, info: dict[str, Any] | None) -> str:
    """One sentence under the picker about the chosen name."""
    if not info:
        return f"{name}: a futtató nem küldött róla részleteket."
    head = (info.get("description") or "").rstrip(" .!?")
    body = ", ".join(p for p in (info.get("target_label") or "", where_text(info), thinking_text(info)) if p)
    off = offline_text(info) if info.get("where") == "local" else ""
    sentence = f"{name}: " + ". ".join(p for p in (head, body[:1].upper() + body[1:] if body else "") if p)
    if off:
        sentence += f". Ha a GPU-gép ki van kapcsolva: {off}"
    return sentence + "."


def picker(names: list[str], info: dict[str, dict[str, Any]], default: str | None) -> dict[str, Any]:
    """What a model picker shows: options with text and hint, and the default's hint."""
    return {"options": [{"name": n, "text": short(n, info.get(n)), "hint": hint(n, info.get(n))} for n in names],
            "default_name": default or "", "default_hint": hint(default, info.get(default)) if default else ""}
