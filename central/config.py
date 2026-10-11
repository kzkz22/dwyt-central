"""Settings from environment variables (/etc/dwyt-central/central.env in production)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _bool(name: str, default: bool) -> bool:
    value = _env(name)
    return default if not value else value.lower() in ("1", "true", "yes", "on")


@dataclass
class Settings:
    # Two roles: the app role is subject to row-level security; the system role owns the
    # tables (migrations, sign-in, runner protocol, admin) and is used only in narrow code paths.
    database_url: str = field(default_factory=lambda: _env("DATABASE_URL"))
    database_system_url: str = field(default_factory=lambda: _env("DATABASE_SYSTEM_URL"))
    app_db_role: str = field(default_factory=lambda: _env("APP_DB_ROLE", "dwyt_app"))
    # The public address, e.g. https://dwyt.iodate.date (Google redirect, invite links)
    base_url: str = field(default_factory=lambda: _env("BASE_URL", "http://localhost:8000").rstrip("/"))
    cookie_secure: bool = field(default_factory=lambda: _bool("COOKIE_SECURE", True))
    session_days: float = field(default_factory=lambda: float(_env("SESSION_DAYS", "30")))
    timezone: str = field(default_factory=lambda: _env("TIMEZONE", "Europe/Budapest"))
    # Sign-in
    registration: str = field(default_factory=lambda: _env("REGISTRATION", "invite").lower())  # open|invite|closed
    invite_days: float = field(default_factory=lambda: float(_env("INVITE_DAYS", "7")))
    google_client_id: str = field(default_factory=lambda: _env("GOOGLE_CLIENT_ID"))
    google_client_secret: str = field(default_factory=lambda: _env("GOOGLE_CLIENT_SECRET"))
    google_auth_url: str = field(default_factory=lambda: _env(
        "GOOGLE_AUTH_URL", "https://accounts.google.com/o/oauth2/v2/auth"))
    google_token_url: str = field(default_factory=lambda: _env(
        "GOOGLE_TOKEN_URL", "https://oauth2.googleapis.com/token"))
    # Runner protocol
    lease_s: int = field(default_factory=lambda: int(_env("LEASE_S", "900")))
    claim_wait_s: float = field(default_factory=lambda: float(_env("CLAIM_WAIT_S", "25")))
    claim_poll_s: float = field(default_factory=lambda: float(_env("CLAIM_POLL_S", "1")))
    job_max_attempts: int = field(default_factory=lambda: int(_env("JOB_MAX_ATTEMPTS", "6")))
    pair_minutes: int = field(default_factory=lambda: int(_env("PAIR_MINUTES", "10")))

    # Search: the embedding service (dwyt-central-embed, bge-m3 on the CPU); empty = text search only
    embed_url: str = field(default_factory=lambda: _env("EMBED_URL", "http://127.0.0.1:8001").rstrip("/"))
    embed_model: str = field(default_factory=lambda: _env("EMBED_MODEL", "BAAI/bge-m3"))
    embed_threads: int = field(default_factory=lambda: int(_env("EMBED_THREADS", "2")))
    # cosine similarity a meaning-based hit needs (bge-m3: related ~0.5-0.7, unrelated ~0.3-0.4)
    search_min_similarity: float = field(default_factory=lambda: float(_env("SEARCH_MIN_SIMILARITY", "0.45")))
    # the indexer thread inside the web service (false only in tests)
    indexer: bool = field(default_factory=lambda: _bool("INDEXER", True))

    @property
    def redirect_uri(self) -> str:
        return f"{self.base_url}/auth/google/callback"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
