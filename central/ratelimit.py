"""In-process rate limits for the endpoints anyone on the internet can call without signing in
(and a few per-user ones, keyed by the user id).

Keyed by the client address, which is the real one only when uvicorn trusts the Apache host
(FORWARDED_ALLOW_IPS); otherwise every visitor shares the Apache address and the limits are
global. Single process (uvicorn --workers 1), so a dict is enough.
"""
from __future__ import annotations

import threading
import time
from collections import deque

# name: (max requests, window seconds)
LIMITS = {
    "admin-login": (10, 300),     # password and code attempts together, per address
    "google-start": (30, 300),    # each start stores a sign-in state
    "invite": (30, 300),          # invitation link checks
    "pair-start": (10, 600),      # each start stores a pending pairing
    "mcp": (300, 300),            # MCP calls per token (a model may call several tools per answer)
    "mcp-bad-token": (20, 300),   # wrong MCP tokens per address
    "ask": (60, 3600),            # questions per user (each one is a job for his runner)
    "share": (30, 3600),          # runner share offers per user (each one looks up an e-mail address)
    "install-redeem": (20, 600),  # install codes tried per address (8 characters, 5 minutes, single use)
    "install-code": (20, 3600),   # install commands asked for per user
    "runner-settings": (60, 3600),  # remote settings saves per user
}

_hits: dict[tuple[str, str], deque[float]] = {}
_lock = threading.Lock()


def hit(name: str, key: str | None, now: float | None = None) -> int:
    """Count one request; returns 0 when allowed, else the seconds until the next one is."""
    limit, window = LIMITS[name]
    now = time.monotonic() if now is None else now
    with _lock:
        q = _hits.setdefault((name, key or "?"), deque())
        while q and now - q[0] >= window:
            q.popleft()
        if len(q) >= limit:
            return int(window - (now - q[0])) + 1
        q.append(now)
        if len(_hits) > 50_000:  # forget idle keys
            for k in [k for k, v in _hits.items() if not v or now - v[-1] >= LIMITS[k[0]][1]]:
                del _hits[k]
        return 0


def reset() -> None:
    with _lock:
        _hits.clear()
