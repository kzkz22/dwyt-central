"""Client of the embedding service (central.embedder)."""
from __future__ import annotations

import httpx


class EmbedUnavailable(Exception):
    pass


def embed(url: str, texts: list[str], timeout: float = 120) -> tuple[str, list[list[float]]]:
    """Vectors for texts, and the name of the model that made them."""
    if not url:
        raise EmbedUnavailable("A jelentés szerinti keresés ki van kapcsolva (EMBED_URL üres).")
    try:
        resp = httpx.post(f"{url}/embed", json={"texts": texts}, timeout=httpx.Timeout(timeout, connect=3))
    except httpx.HTTPError as exc:
        raise EmbedUnavailable(f"A beágyazó szolgáltatás nem érhető el ({url}): {exc}") from exc
    if resp.status_code != 200:
        raise EmbedUnavailable(f"A beágyazó szolgáltatás most nem válaszol (HTTP {resp.status_code}): "
                               f"{resp.text[:200]}")
    data = resp.json()
    return data["model"], data["vectors"]


def health(url: str) -> dict | None:
    if not url:
        return None
    try:
        return httpx.get(f"{url}/health", timeout=3).json()
    except (httpx.HTTPError, ValueError):
        return None
