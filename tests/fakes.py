"""A stand-in for Google's OAuth endpoints (only the token endpoint is called by the server;
the authorization step is played by the test itself)."""
from __future__ import annotations

import base64
import hashlib
import json
import socket
import threading
import time

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _b64(data: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()


class FakeGoogle:
    """codes: code -> {"challenge", "nonce", "claims"}; the test registers one before the callback."""

    def __init__(self, client_id: str) -> None:
        self.client_id = client_id
        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.reset()
        app = FastAPI()
        fake = self

        @app.post("/token")
        async def token(request: Request):
            form = await request.form()
            if form.get("grant_type") == "refresh_token":
                if form.get("refresh_token") not in fake.refresh_tokens or form.get("client_secret") != "gsecret":
                    return JSONResponse({"error": "invalid_grant"}, status_code=400)
                return {"access_token": f"at-{form.get('refresh_token')}", "expires_in": 3600}
            entry = fake.codes.pop(str(form.get("code")), None)
            if entry is None or form.get("client_id") != fake.client_id or form.get("client_secret") != "gsecret":
                return JSONResponse({"error": "invalid_grant"}, status_code=400)
            verifier = str(form.get("code_verifier") or "")
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
            if challenge != entry["challenge"]:
                return JSONResponse({"error": "invalid_grant", "error_description": "PKCE"}, status_code=400)
            claims = {"iss": "https://accounts.google.com", "aud": fake.client_id, "exp": time.time() + 3600,
                      "nonce": entry["nonce"], **entry["claims"]}
            id_token = f"{_b64({'alg': 'RS256'})}.{_b64(claims)}.sig"
            fake.calls += 1
            answer = {"access_token": "at", "id_token": id_token, "token_type": "Bearer", "expires_in": 3600,
                      "scope": entry.get("scope", "openid email profile")}
            if entry.get("refresh_token"):
                answer["refresh_token"] = entry["refresh_token"]
                fake.refresh_tokens.add(entry["refresh_token"])
            return answer

        @app.post("/revoke")
        async def revoke(request: Request):
            fake.revoked.append(str((await request.form()).get("token")))
            return {}

        @app.get("/youtube/v3/subscriptions")
        async def subscriptions(request: Request):
            token = request.headers.get("authorization", "").removeprefix("Bearer at-")
            if token not in fake.refresh_tokens:
                return JSONResponse({"error": "unauthorized"}, status_code=401)
            subs = fake.subscriptions.get(token, [])
            start = int(request.query_params.get("pageToken") or 0)
            page = subs[start:start + 2]  # small pages: the client must follow nextPageToken
            body = {"items": [{"snippet": {"title": t, "resourceId": {"channelId": c}}} for c, t in page]}
            if start + 2 < len(subs):
                body["nextPageToken"] = str(start + 2)
            return body

        self.app = app
        self._server: uvicorn.Server | None = None

    def reset(self) -> None:
        self.codes: dict[str, dict] = {}
        self.calls = 0
        self.refresh_tokens: set[str] = set()
        self.revoked: list[str] = []
        self.subscriptions: dict[str, list[tuple[str, str]]] = {}  # refresh token -> (channel id, title)

    def start(self) -> "FakeGoogle":
        self._server = uvicorn.Server(uvicorn.Config(self.app, host="127.0.0.1", port=self.port, log_level="warning"))
        threading.Thread(target=self._server.run, daemon=True).start()
        for _ in range(100):
            try:
                socket.create_connection(("127.0.0.1", self.port), timeout=0.1).close()
                break
            except OSError:
                time.sleep(0.05)
        return self

    def stop(self) -> None:
        if self._server:
            self._server.should_exit = True


SYNONYMS = {"garden": "kert", "gardening": "kert", "kertészet": "kert", "kertész": "kert", "kertről": "kert",
            "bees": "méh", "méhek": "méh", "train": "vasút", "railway": "vasút", "vonat": "vasút"}


def fake_vector(text: str, dim: int = 1024) -> list[float]:
    """Deterministic stand-in for bge-m3: a normalised bag of word stems, with a few
    cross-language synonyms, so related texts get a high cosine similarity."""
    import math
    import re
    vec = [0.0] * dim
    for word in re.findall(r"\w+", text.lower()):
        word = SYNONYMS.get(word, word)
        if len(word) < 3:
            continue
        vec[int(hashlib.sha256(word[:5].encode()).hexdigest(), 16) % dim] += 1.0
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / norm for x in vec]


class FakeEmbed:
    """A stand-in for the embedding service (central.embedder). down = answer 503."""

    def __init__(self) -> None:
        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.reset()
        app = FastAPI()
        fake = self

        @app.post("/embed")
        async def embed(request: Request):
            body = await request.json()
            fake.calls.append(body["texts"])
            if fake.down:
                return JSONResponse({"detail": "loading"}, status_code=503)
            return {"model": "BAAI/bge-m3", "vectors": [fake_vector(t) for t in body["texts"]]}

        self.app = app
        self._server: uvicorn.Server | None = None

    def reset(self) -> None:
        self.down = False
        self.calls: list[list[str]] = []

    start = FakeGoogle.start
    stop = FakeGoogle.stop


class FakeYoutube:
    """YouTube's public pages the central service reads: channel feeds, the Shorts address, @handle pages.
    feeds: channel id -> [(video id, title, published ISO)]; shorts: video ids; handles: @name -> (id, title)."""

    def __init__(self) -> None:
        from fastapi.responses import HTMLResponse, Response
        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.reset()
        app = FastAPI()
        fake = self

        @app.get("/feeds/videos.xml")
        async def feed(channel_id: str):
            fake.feed_calls.append(channel_id)
            if channel_id not in fake.feeds:
                return Response(status_code=404)
            entries = "".join(
                f"<entry><yt:videoId>{vid}</yt:videoId><title>{title}</title><published>{when}</published></entry>"
                for vid, title, when in fake.feeds[channel_id])
            xml = ('<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom" '
                   'xmlns:yt="http://www.youtube.com/xml/schemas/2015">'
                   f"<title>{fake.titles.get(channel_id, 'Csatorna')}</title>{entries}</feed>")
            return Response(xml, media_type="application/atom+xml")

        @app.head("/shorts/{video_id}")
        async def shorts(video_id: str):
            if video_id in fake.shorts:
                return Response(status_code=200)
            return Response(status_code=303, headers={"location": f"/watch?v={video_id}"})

        @app.get("/{handle}")
        async def handle_page(handle: str):
            if handle not in fake.handles:
                return Response(status_code=404)
            cid, title = fake.handles[handle]
            return HTMLResponse(f'<html><head><meta property="og:title" content="{title}">'
                                f'<link rel="canonical" href="https://www.youtube.com/channel/{cid}"></head>'
                                f'<body>"externalId":"{cid}"</body></html>')

        self.app = app
        self._server: uvicorn.Server | None = None

    def reset(self) -> None:
        self.feeds: dict[str, list[tuple[str, str, str]]] = {}
        self.titles: dict[str, str] = {}
        self.shorts: set[str] = set()
        self.handles: dict[str, tuple[str, str]] = {}
        self.feed_calls: list[str] = []

    start = FakeGoogle.start
    stop = FakeGoogle.stop


class FakeNotify:
    """ntfy (JSON publish at the root) and Home Assistant (notify services) in one fake."""

    def __init__(self) -> None:
        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.reset()
        app = FastAPI()
        fake = self

        @app.post("/")
        async def ntfy(request: Request):
            if fake.fail:
                return JSONResponse({"error": "down"}, status_code=503)
            fake.ntfy.append({"body": await request.json(), "auth": request.headers.get("authorization")})
            return {"id": "x"}

        @app.post("/api/services/notify/{service}")
        async def ha(service: str, request: Request):
            if request.headers.get("authorization") != "Bearer ha-token":
                return JSONResponse({"message": "unauthorized"}, status_code=401)
            fake.ha.append({"service": service, "body": await request.json()})
            return []

        self.app = app
        self._server: uvicorn.Server | None = None

    def reset(self) -> None:
        self.ntfy: list[dict] = []
        self.ha: list[dict] = []
        self.fail = False

    start = FakeGoogle.start
    stop = FakeGoogle.stop
