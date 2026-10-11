"""Test setup: a throw-away PostgreSQL (tests/pgcluster.py) or TEST_DATABASE_URL (a superuser URL),
with the two roles of production: the table owner (migrations, system code) and the app role
(subject to row-level security). Plus a fake Google token endpoint and protocol helpers."""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from sqlalchemy import create_engine, text

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fakes import FakeEmbed, FakeGoogle  # noqa: E402
from pgcluster import Cluster  # noqa: E402

PROTO = {"Dwyt-Protocol": "1"}
TABLES = ("index_queue, chunks, item_translations, translation_segments, translations, "
          "jobs, item_summaries, item_transcripts, items, summaries, segments, transcripts, sources, "
          "pair_requests, runners, login_states, sessions, invites, users")


@pytest.fixture(scope="session")
def database_urls():
    given = os.environ.get("TEST_DATABASE_URL")
    cluster = None
    if given:
        admin_url = given
    else:
        cluster = Cluster()
        admin_url = cluster.start()
    try:
        engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
        with engine.connect() as conn:
            conn.execute(text("DROP DATABASE IF EXISTS dwyt_test"))
            for role in ("dwyt_test_owner", "dwyt_test_app"):
                conn.execute(text(f"DROP ROLE IF EXISTS {role}"))
            conn.execute(text("CREATE ROLE dwyt_test_owner LOGIN PASSWORD 'owner'"))
            conn.execute(text("CREATE ROLE dwyt_test_app LOGIN PASSWORD 'app'"))
            conn.execute(text("CREATE DATABASE dwyt_test OWNER dwyt_test_owner ENCODING 'UTF8' TEMPLATE template0"))
        engine.dispose()
        # extensions need a superuser once, as in production (docs/frissites.md)
        db_engine = create_engine(admin_url.rsplit("/", 1)[0] + "/dwyt_test", isolation_level="AUTOCOMMIT")
        with db_engine.connect() as conn:
            for ext in ("vector", "unaccent", "pg_trgm"):
                conn.execute(text(f"CREATE EXTENSION IF NOT EXISTS {ext}"))
        db_engine.dispose()
        parts = urlsplit(admin_url)
        hostport = parts.hostname + (f":{parts.port}" if parts.port else "")
        yield (f"postgresql+psycopg://dwyt_test_owner:owner@{hostport}/dwyt_test",
               f"postgresql+psycopg://dwyt_test_app:app@{hostport}/dwyt_test")
    finally:
        if cluster:
            cluster.stop()


@pytest.fixture(scope="session")
def fake_google():
    g = FakeGoogle("gclient").start()
    yield g
    g.stop()


@pytest.fixture(scope="session")
def fake_embed():
    e = FakeEmbed().start()
    yield e
    e.stop()


@pytest.fixture(scope="session")
def configured(database_urls, fake_google):
    system_url, app_url = database_urls
    os.environ.update(DATABASE_SYSTEM_URL=system_url, DATABASE_URL=app_url, APP_DB_ROLE="dwyt_test_app",
                      BASE_URL="http://testserver", COOKIE_SECURE="false", GOOGLE_CLIENT_ID="gclient",
                      GOOGLE_CLIENT_SECRET="gsecret", GOOGLE_TOKEN_URL=f"{fake_google.url}/token",
                      GOOGLE_AUTH_URL="https://accounts.example/auth", REGISTRATION="invite",
                      CLAIM_WAIT_S="0.3", CLAIM_POLL_S="0.05", LEASE_S="900",
                      INDEXER="false", EMBED_URL="")
    from alembic import command
    from alembic.config import Config

    from central import db
    from central.config import get_settings

    get_settings.cache_clear()
    db.reset()
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    command.upgrade(cfg, "head")
    return cfg


@pytest.fixture(autouse=True)
def clean(configured, fake_google, monkeypatch):
    from central import db, web
    from central.config import get_settings

    for key in ("REGISTRATION", "CLAIM_WAIT_S"):
        monkeypatch.setenv(key, os.environ[key])
    get_settings.cache_clear()
    with db.system_engine().begin() as conn:
        conn.execute(text(f"TRUNCATE {TABLES} RESTART IDENTITY CASCADE"))
    fake_google.reset()
    web._pair_failures.clear()
    from central import ratelimit
    ratelimit.reset()
    yield
    get_settings.cache_clear()


@pytest.fixture
def anon(configured):
    from fastapi.testclient import TestClient

    from central.web import app
    with TestClient(app, follow_redirects=False) as c:
        yield c


def csrf_of(html: str) -> str:
    found = re.search(r'name="csrf_token" value="([^"]+)"', html) or re.search(r'data-csrf="([^"]+)"', html)
    assert found, "no CSRF token on the page"
    return found.group(1)


def google_login(client, fake_google, email: str, sub: str, name: str = "Teszt", invite: str = "",
                 verified: bool = True):
    """Run the whole Google sign-in; returns the callback response."""
    r = client.get("/auth/google/start" + (f"?meghivo={invite}" if invite else ""))
    assert r.status_code == 302, r.text
    q = parse_qs(urlsplit(r.headers["location"]).query)
    code = f"code-{sub}"
    fake_google.codes[code] = {"challenge": q["code_challenge"][0], "nonce": q["nonce"][0],
                               "claims": {"sub": sub, "email": email, "email_verified": verified, "name": name}}
    return client.get(f"/auth/google/callback?state={q['state'][0]}&code={code}")


def make_admin(email: str = "admin@example.com", password: str = "nagyon-titkos-jelszo"):
    from central import cli
    cli._show_totp = lambda user: None  # noqa: SLF001 (quiet in tests)
    cli.create_admin(email, password)


def make_user(client_factory, fake_google, email: str, sub: str):
    """A signed-in user with its own client (registered through an invitation)."""
    from central import accounts, db
    from central.config import get_settings
    with db.system_session() as s:
        link = accounts.create_invite(s, get_settings(), email, None)
    client = client_factory()
    r = google_login(client, fake_google, email, sub, invite=link.rsplit("/", 1)[1])
    assert r.status_code == 303, r.text
    client.csrf = csrf_of(client.get("/").text)
    return client


@pytest.fixture
def client_factory(configured):
    from fastapi.testclient import TestClient

    from central.web import app
    clients = []

    def make():
        c = TestClient(app, follow_redirects=False)
        c.__enter__()
        clients.append(c)
        return c
    yield make
    for c in clients:
        c.__exit__(None, None, None)


@pytest.fixture
def alice(client_factory, fake_google):
    return make_user(client_factory, fake_google, "alice@example.com", "sub-alice")


@pytest.fixture
def bob(client_factory, fake_google):
    return make_user(client_factory, fake_google, "bob@example.com", "sub-bob")


def post(c, url: str, data: dict | None = None, **kw):
    return c.post(url, data={**(data or {}), "csrf_token": c.csrf}, **kw)


def pair_runner(client, runner_client, name: str = "otthoni gép") -> str:
    """Pair a runner for the user of `client`; returns the runner token."""
    start = runner_client.post("/runner/v1/pair/start", headers=PROTO).json()
    pending = runner_client.post("/runner/v1/pair/poll", json={"poll_id": start["poll_id"]}, headers=PROTO)
    assert pending.status_code == 202
    r = post(client, "/futtatok/parositas", {"code": start["code"].lower().replace("-", " "), "name": name})
    assert r.status_code == 200 and "Jóváhagyva" in r.text, r.text
    done = runner_client.post("/runner/v1/pair/poll", json={"poll_id": start["poll_id"]}, headers=PROTO)
    assert done.status_code == 200, done.text
    return done.json()["token"]


CAPS = {"kinds": ["transcribe", "summarize", "translate"], "transcribe_modes": ["captions", "cloud"],
        "llm_roles": ["public", "private"], "allowed_domains": ["youtube.com", "youtu.be"]}


def runner_headers(token: str) -> dict:
    return {**PROTO, "Authorization": f"Bearer {token}"}


def claim(runner_client, token: str, caps: dict | None = None):
    return runner_client.post("/runner/v1/claim", json={"capabilities": caps or CAPS, "runner_version": "0.1.0",
                                                          "tool_versions": {"yt-dlp": "2026.08.19"}},
                              headers=runner_headers(token))


def transcript_result(texts=("Jó napot kívánok.", "Ma a kertről beszélünk."), mode="captions", language="hu",
                      title="Kerti videó", channel="Kertész") -> dict:
    return {"video": {"title": title, "channel": channel, "duration_s": 120},
            "transcript": {"mode": mode, "model": "youtube-auto" if mode == "captions" else "universal-2",
                           "caption_kind": "auto" if mode == "captions" else None, "language": language,
                           "diarized": mode != "captions",
                           "speakers": [] if mode == "captions" else [{"label": "Szereplő 1", "share": 1.0}],
                           "warnings": [], "segments": [
                               {"start": i * 5.0, "end": i * 5.0 + 4, "text": t,
                                "speaker": None if mode == "captions" else "Szereplő 1"}
                               for i, t in enumerate(texts)]}}
