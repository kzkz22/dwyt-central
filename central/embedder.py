"""The embedding service:  uvicorn central.embedder:app --host 127.0.0.1 --port 8001

Loads the embedding model (EMBED_MODEL, default BAAI/bge-m3) once at start, on the CPU, fp32,
with EMBED_THREADS threads, and keeps it in memory (~2.2 GB). The web service asks it for query
vectors (search) and its indexer for passage vectors. Requests are served one at a time; the
indexer sends small batches so a search never waits long behind indexing.
(Ported from yt-transcribe-webui.) Needs the [embed] extra: torch (CPU build), sentence-transformers.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from .config import get_settings

log = logging.getLogger("dwyt.embed")
_state: dict = {"model": None, "error": None}
MAX_TOKENS = 1024
_lock = threading.Lock()


def _load() -> None:
    settings = get_settings()
    try:
        import torch
        from sentence_transformers import SentenceTransformer

        torch.set_num_threads(settings.embed_threads)
        started = time.monotonic()
        model = SentenceTransformer(settings.embed_model, device="cpu")
        # chunks are ~700 tokens; a cap keeps CPU attention memory small for long summaries
        model.max_seq_length = min(getattr(model, "max_seq_length", MAX_TOKENS) or MAX_TOKENS, MAX_TOKENS)
        _state["model"] = model
        log.info("embedding model %s loaded in %.0f s", settings.embed_model, time.monotonic() - started)
    except Exception as exc:  # noqa: BLE001
        log.exception("could not load the embedding model")
        _state["error"] = f"{type(exc).__name__}: {exc}"
        os._exit(1)  # let systemd restart the service (Restart=on-failure)


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    threading.Thread(target=_load, daemon=True, name="load-model").start()
    yield


app = FastAPI(title="dwyt-central-embed", docs_url=None, redoc_url=None, openapi_url=None, lifespan=_lifespan)


class EmbedRequest(BaseModel):
    texts: list[str] = Field(min_length=1, max_length=64)


@app.get("/health")
def health() -> dict:
    return {"ok": _state["model"] is not None, "model": get_settings().embed_model,
            "loading": _state["model"] is None and _state["error"] is None, "error": _state["error"]}


@app.post("/embed")
def embed(req: EmbedRequest) -> dict:
    model = _state["model"]
    if model is None:
        raise HTTPException(503, _state["error"] or "the model is still loading")
    with _lock:
        vectors = model.encode([t[:20000] for t in req.texts], normalize_embeddings=True,
                               batch_size=len(req.texts))
    return {"model": get_settings().embed_model, "vectors": [[float(x) for x in v] for v in vectors]}
