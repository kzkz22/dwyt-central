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
            return {"access_token": "at", "id_token": id_token, "token_type": "Bearer", "expires_in": 3600}

        self.app = app
        self._server: uvicorn.Server | None = None

    def reset(self) -> None:
        self.codes: dict[str, dict] = {}
        self.calls = 0

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
