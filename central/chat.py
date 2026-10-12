"""Questions about one item or the whole library, answered by the user's runner (plan 15a).

The central service gathers the background itself (the item's transcript with its translation and
summary, or the best passages of a library search) and hands it to the runner with the
conversation so far, as an `ask` job. The model gets no tools: a transcript comes from a video and
is untrusted text, so the worst a planted instruction can do is a misleading answer.

Own recordings never go into a library question's background; on an own recording's page the
question goes out with the private role (the runner's local model), like its summary.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session as DbSession

from . import db, jobs, library, personal, ratelimit, render, search
from .config import Settings
from .db import utcnow
from .models import (ACTIVE, Conversation, ConversationMessage, Item, Job, Runner, Source, Summary, Transcript,
                     Translation)

MAX_QUESTION = 4000
MAX_HISTORY = 20            # messages of the conversation sent with a question
MAX_HISTORY_CHARS = 40_000
ITEM_MAX_CHARS = 250_000    # longer transcripts: the item's best passages for the question
EXCERPT_HITS = 40
LIBRARY_MAX_ITEMS = 8
LIBRARY_MAX_CHARS = 60_000
SUMMARY_MAX_CHARS = 6_000
CATALOG_MAX_CHARS = 120_000   # the list of the whole library sent with every library question
CATALOG_SUMMARY_CHARS = 400   # at most this much of each summary; less as the library grows
CATALOG_MIN_SUMMARY = 60      # below this the summaries are left out, titles only


class ChatError(Exception):
    """A user-facing reason (Hungarian)."""


# ---- asking (user session) -------------------------------------------------------------------

def _previous_question(user_id: int, conversation_id: int | None) -> str:
    if not conversation_id:
        return ""
    with db.user_session(user_id) as s:
        last = s.scalar(select(ConversationMessage.content).where(
            ConversationMessage.conversation_id == conversation_id, ConversationMessage.role == "user")
            .order_by(ConversationMessage.id.desc()).limit(1))
    return last or ""


def search_terms(question: str, previous: str = "") -> str:
    """A question is not a search phrase: full text search would want every word in one passage.
    Its words (3+ letters) are searched as alternatives instead; the ranking prefers passages with
    more of them, and the meaning search sees the whole question anyway."""
    words = list(dict.fromkeys(re.findall(r"[^\W_]{3,}", f"{question} {previous}".lower())))
    out = ""
    for word in words:
        if len(out) + len(word) + 4 > 290:  # search.run keeps 300 characters
            break
        out = f"{out} or {word}" if out else word
    return out or question


def library_sources(user_id: int, question: str, settings: Settings, previous: str = "") -> list[dict[str, Any]]:
    """The background of a library question: the best passages of the hybrid search, own
    recordings left out. A follow-up question is searched together with the previous one."""
    query = search_terms(question, previous if len(question) < 80 else "")
    outcome = search.run(user_id, query, settings, filters=search.Filters(public_only=True), hits_per_item=4,
                         meaning_text=f"{previous} {question}".strip() if query != question else None)
    out: list[dict[str, Any]] = []
    used = 0
    with db.user_session(user_id) as s:
        for res in outcome.results[:LIBRARY_MAX_ITEMS]:
            item = s.get(Item, res.item["id"])
            if item is None or not item.is_public:
                continue
            excerpts, summary = [], None
            for hit in res.hits:
                if hit.kind == "transcript" and hit.start_s is not None:
                    excerpts.append({"start": float(hit.start_s), "text": hit.text, "hu": hit.text_hu})
                elif hit.kind == "summary" and item.current_summary_id:
                    found = s.get(Summary, item.current_summary_id)
                    summary = found.text[:SUMMARY_MAX_CHARS] if found else None
            size = sum(len(e["text"]) + len(e["hu"] or "") for e in excerpts) + len(summary or "")
            if out and used + size > LIBRARY_MAX_CHARS:
                break
            used += size
            src = item.source
            out.append({"n": len(out) + 1, "item_id": item.id, "title": library.item_title(item),
                        "channel": (src.meta or {}).get("channel"), "url": src.url,
                        "youtube": src.key.split(":", 1)[1] if src.kind == "youtube" else None,
                        "language": res.item.get("language"), "summary": summary,
                        "excerpts": sorted(excerpts, key=lambda e: e["start"])})
    return out


def library_catalog(user_id: int, sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The whole library (own recordings left out) as a short list, so that overview questions
    ("group all my videos by topic") see every item, not only the search hits. The items already
    among the search sources keep their number; the others are numbered after them. Every entry
    gets the same share of CATALOG_MAX_CHARS for the start of its summary; with a very large
    library only titles remain, and past that the newest items (catalog_partial in job_input)."""
    in_sources = {int(src["item_id"]): int(src["n"]) for src in sources if src.get("item_id")}
    with db.user_session(user_id) as s:
        found = list(s.scalars(select(Item).where(Item.is_public.is_(True), Item.main_transcript_id.is_not(None))
                               .order_by(Item.created_at.desc(), Item.id.desc())))
        tags = personal.tags_by_item(s, [i.id for i in found])
        summaries = {sm.id: sm.text for sm in s.scalars(select(Summary).where(
            Summary.id.in_([i.current_summary_id for i in found if i.current_summary_id] or [0])))}
        rows = []
        for item in found:
            src = item.source
            meta = src.meta or {}
            rows.append({"item_id": item.id, "title": library.item_title(item), "channel": meta.get("channel"),
                         "url": src.url, "youtube": src.key.split(":", 1)[1] if src.kind == "youtube" else None,
                         "date": str(meta.get("published") or item.created_at.date().isoformat())[:10],
                         "tags": tags.get(item.id, []), "summary_text": summaries.get(item.current_summary_id)})
    base = sum(len(r["title"] or "") + len(r["channel"] or "") + sum(len(x) + 2 for x in r["tags"]) + 40
               for r in rows)
    per_item = min(CATALOG_SUMMARY_CHARS, (CATALOG_MAX_CHARS - base) // max(1, len(rows)))
    out: list[dict[str, Any]] = []
    used, next_n = 0, max(in_sources.values(), default=0) + 1
    for r in rows:
        text = r.pop("summary_text")
        r["summary"] = render.plain_preview(text, per_item) if text and per_item >= CATALOG_MIN_SUMMARY else None
        used += len(r["title"] or "") + len(r["channel"] or "") + len(r["summary"] or "") + 40
        if out and used > CATALOG_MAX_CHARS:
            break
        if r["item_id"] in in_sources:
            r["n"] = in_sources[r["item_id"]]
        else:
            r["n"], next_n = next_n, next_n + 1
        out.append({**r, "catalog": True})
    return out


def shown_sources(sources: list[dict[str, Any]], answer: str | None) -> list[dict[str, Any]]:
    """The sources listed under an answer: the search sources, and the catalogue entries the answer cites."""
    cited = {int(n) for n in re.findall(r"\[#(\d{1,5})", answer or "")}
    return [x for x in sources or [] if not x.get("catalog") or int(x.get("n") or 0) in cited]


def offered(s: DbSession, user_id: int, role: str) -> list[str]:
    from . import sharing
    return jobs.offered_models(sharing.usable_runners(s, user_id), role)


def can_answer(s: DbSession, user_id: int) -> bool:
    """Has the user a runner (own or shared) that announced the ask kind?"""
    from . import sharing
    return any("ask" in ((r.capabilities or {}).get("kinds") or []) for r in sharing.usable_runners(s, user_id))


def ask(settings: Settings, user_id: int, text: str, item_id: int | None = None,
        conversation_id: int | None = None, model: str = "") -> int:
    """Store the question and queue its job; returns the conversation id."""
    text = (text or "").strip()
    if not text:
        raise ChatError("Írd be a kérdést.")
    if len(text) > MAX_QUESTION:
        raise ChatError(f"A kérdés legfeljebb {MAX_QUESTION} karakter lehet.")
    wait = ratelimit.hit("ask", str(user_id))
    if wait:
        raise ChatError(f"Túl sok kérdés rövid idő alatt; próbáld újra {max(1, wait // 60)} perc múlva.")
    sources: list[dict[str, Any]] = []
    if item_id is None:
        sources = library_sources(user_id, text, settings, _previous_question(user_id, conversation_id))
        catalog = library_catalog(user_id, sources)
        if not sources and not catalog:
            raise ChatError("A könyvtáradban még nincs olyan videó, amelyről kérdezni lehetne (a saját felvételek "
                            "itt kimaradnak). Saját felvételről az elem oldalán kérdezhetsz.")
    with db.user_session(user_id) as s:
        item = None
        if item_id is not None:
            item = s.get(Item, item_id)
            if item is None:
                raise ChatError("Nincs ilyen elem.")
            if item.main_transcript_id is None:
                raise ChatError("Ennek az elemnek még nincs átirata; utána lehet kérdezni róla.")
        role = "public" if item is None or item.is_public else "private"
        if model and model not in offered(s, user_id, role):
            raise ChatError("Ezt a modellt a futtatód nem ajánlja fel.")
        if conversation_id:
            conv = s.get(Conversation, conversation_id)
            if conv is None or conv.item_id != item_id:
                raise ChatError("Nincs ilyen beszélgetés.")
            if pending(s, conv.id) is not None:
                raise ChatError("Várd meg az előző kérdés válaszát.")
        else:
            conv = Conversation(user_id=user_id, item_id=item_id, title=" ".join(text.split())[:80])
            s.add(conv)
            s.flush()
        msg = ConversationMessage(conversation_id=conv.id, user_id=user_id, role="user", content=text,
                                  sources=sources + ([] if item_id is not None else catalog), model=model or None)
        s.add(msg)
        s.flush()
        payload: dict[str, Any] = {"conversation_id": conv.id, "message_id": msg.id, "role": role}
        if model:
            payload["model"] = model
        job = Job(user_id=user_id, item_id=item_id, kind="ask", state="queued", stage="queued",
                  title=f"Kérdés: {' '.join(text.split())[:60]}", payload=payload, progress={})
        s.add(job)
        s.flush()
        msg.job_id = job.id
        conv.updated_at = utcnow()
        return conv.id


def pending(s: DbSession, conversation_id: int) -> Job | None:
    """The job of the conversation's unanswered question, while it is queued or running."""
    return s.scalar(select(Job).join(ConversationMessage, ConversationMessage.job_id == Job.id).where(
        ConversationMessage.conversation_id == conversation_id, Job.state.in_(ACTIVE)).limit(1))


def retry(user_id: int, conversation_id: int) -> bool:
    """Ask the last question again after its job failed."""
    with db.user_session(user_id) as s:
        last = s.scalar(select(ConversationMessage).where(ConversationMessage.conversation_id == conversation_id)
                        .order_by(ConversationMessage.id.desc()).limit(1))
        if last is None or last.role != "user" or last.job_id is None:
            return False
        job = s.get(Job, last.job_id)
        if job is None or job.state not in ("failed", "cancelled"):
            return False
        jobs.retry(s, job)
        return True


def delete(user_id: int, conversation_id: int) -> int | None:
    """Delete a conversation (its running question is cancelled); returns its item id."""
    with db.user_session(user_id) as s:
        conv = s.get(Conversation, conversation_id)
        if conv is None:
            return None
        item_id = conv.item_id
        job = pending(s, conv.id)
        if job is not None:
            jobs.cancel(s, job)
        s.delete(conv)
        return item_id


# ---- reading (user session) ------------------------------------------------------------------

@dataclass
class Turn:
    question: ConversationMessage
    answer: ConversationMessage | None
    job: Job | None


def conversations(s: DbSession, item_id: int | None, limit: int = 20) -> list[Conversation]:
    q = select(Conversation).order_by(Conversation.updated_at.desc()).limit(limit)
    q = q.where(Conversation.item_id.is_(None)) if item_id is None else q.where(Conversation.item_id == item_id)
    return list(s.scalars(q))


def turns(s: DbSession, conversation_id: int) -> list[Turn]:
    msgs = list(s.scalars(select(ConversationMessage).where(ConversationMessage.conversation_id == conversation_id)
                          .order_by(ConversationMessage.id)))
    job_ids = [m.job_id for m in msgs if m.job_id]
    found = {j.id: j for j in s.scalars(select(Job).where(Job.id.in_(job_ids)))} if job_ids else {}
    out: list[Turn] = []
    for m in msgs:
        if m.role == "user":
            out.append(Turn(m, None, found.get(m.job_id) if m.job_id else None))
        elif out and out[-1].answer is None:
            out[-1].answer = m
    return out


def markdown(s: DbSession, conv: Conversation, title: str, when: datetime | None = None) -> str:
    """The whole conversation as Markdown (the download)."""
    from zoneinfo import ZoneInfo

    from .config import get_settings
    tz = ZoneInfo(get_settings().timezone)

    def at(value: datetime | None) -> str:
        return value.astimezone(tz).strftime("%Y-%m-%d %H:%M") if value else ""
    lines = [f"# {conv.title}", "", f"- Miről: {title}", f"- Kezdve: {at(conv.created_at)}", ""]
    for i, turn in enumerate(turns(s, conv.id), 1):
        lines += [f"## {i}. kérdés ({at(turn.question.created_at)})", "", turn.question.content, ""]
        if turn.answer is not None:
            a = turn.answer
            lines += [f"### Válasz ({a.model_used or a.model or 'modell'}, {at(a.created_at)})", "", a.content, ""]
        elif turn.job is not None and turn.job.state == "failed":
            lines += [f"*Nem jött válasz: {turn.job.error or 'hiba'}*", ""]
        shown = shown_sources(turn.question.sources, turn.answer.content if turn.answer else None)
        if shown:
            lines += ["Források:", ""]
            for src in shown:
                link = src.get("url") or f"/elem/{src.get('item_id')}"
                lines.append(f"- #{src.get('n')} {src.get('title')}: {link}")
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# ---- the runner side (system session) --------------------------------------------------------

def _history(s: DbSession, conv_id: int, upto: int) -> list[dict[str, str]]:
    msgs = list(s.scalars(select(ConversationMessage).where(ConversationMessage.conversation_id == conv_id,
                                                           ConversationMessage.id <= upto)
                          .order_by(ConversationMessage.id)))
    pairs: list[dict[str, str]] = []
    for i, m in enumerate(msgs):
        if m.role == "user" and m.id != upto and not (i + 1 < len(msgs) and msgs[i + 1].role == "assistant"):
            continue  # a question that got no answer
        pairs.append({"role": m.role, "content": m.content})
    pairs = pairs[-MAX_HISTORY:]
    while len(pairs) > 1 and sum(len(p["content"]) for p in pairs) > MAX_HISTORY_CHARS:
        pairs.pop(0)
    while pairs and pairs[0]["role"] != "user":
        pairs.pop(0)
    return pairs


def _item_source(s: DbSession, settings: Settings, user_id: int, item: Item, question: str) -> dict[str, Any]:
    t = s.get(Transcript, item.main_transcript_id)
    if t is None:
        raise jobs.ProtocolError(410, "transcript gone")
    names = personal.names_for(s, item.id, t.id)
    tr = s.get(Translation, item.current_translation_id) if item.current_translation_id else None
    hu = {g.idx: g.text for g in tr.segments} if tr is not None and tr.transcript_id == t.id else {}
    lines = [{"start": g.start_s, "speaker": personal.rename(g.speaker, names), "text": g.text, "hu": hu.get(g.idx)}
             for g in t.segments]
    partial = sum(len(x["text"]) + len(x["hu"] or "") for x in lines) > ITEM_MAX_CHARS
    if partial:
        outcome = search.run(user_id, search_terms(question), settings, filters=search.Filters(item_id=item.id),
                             hits_per_item=EXCERPT_HITS, meaning_text=question)
        picked = [{"start": float(h.start_s), "speaker": None, "text": h.text, "hu": h.text_hu}
                  for r in outcome.results for h in r.hits if h.kind == "transcript" and h.start_s is not None]
        if picked:
            lines = sorted(picked, key=lambda x: x["start"])
        else:  # nothing matched: the beginning, as much as fits
            kept, size = [], 0
            for x in lines:
                size += len(x["text"]) + len(x["hu"] or "")
                if size > ITEM_MAX_CHARS:
                    break
                kept.append(x)
            lines = kept
    summary = s.get(Summary, item.current_summary_id) if item.current_summary_id else None
    src: Source = s.get(Source, item.source_id)
    return {"n": 1, "title": library.item_title(item), "channel": (src.meta or {}).get("channel"), "url": src.url,
            "language": t.language, "diarized": t.diarized, "partial": partial,
            "summary": personal.rename_text(summary.text, names)[:SUMMARY_MAX_CHARS] if summary else None,
            "transcript": lines}


def job_input(s: DbSession, job: Job, settings: Settings) -> dict[str, Any]:
    msg = s.get(ConversationMessage, job.payload["message_id"])
    conv = s.get(Conversation, job.payload["conversation_id"])
    if msg is None or conv is None:
        raise jobs.ProtocolError(410, "the conversation was deleted")
    if conv.item_id is not None:
        item = s.get(Item, conv.item_id)
        if item is None or item.main_transcript_id is None:
            raise jobs.ProtocolError(410, "the item was deleted")
        sources = [_item_source(s, settings, job.user_id, item, msg.content)]
    else:
        sources = [{"n": src["n"], "title": src.get("title"), "channel": src.get("channel"), "url": src.get("url"),
                    "language": src.get("language"), "summary": src.get("summary"), "partial": True,
                    "transcript": [{"start": e["start"], "speaker": None, "text": e["text"], "hu": e.get("hu")}
                                   for e in src.get("excerpts") or []]}
                   for src in msg.sources if not src.get("catalog")]
    chosen = {"model": job.payload["model"]} if job.payload.get("model") else {}
    data = {**chosen, "scope": "item" if conv.item_id is not None else "library", "role": job.payload["role"],
            "language": "hu", "sources": sources, "messages": _history(s, conv.id, msg.id)}
    if conv.item_id is None:
        catalog = [x for x in msg.sources if x.get("catalog")]
        total = s.scalar(select(func.count()).select_from(Item).where(
            Item.user_id == job.user_id, Item.is_public.is_(True), Item.main_transcript_id.is_not(None))) or 0
        data["catalog"] = [{"n": x["n"], "title": x.get("title"), "channel": x.get("channel"), "date": x.get("date"),
                            "tags": x.get("tags") or [], "summary": x.get("summary")} for x in catalog]
        data["library_total"] = max(total, len(catalog))
    return data


def store_answer(s: DbSession, runner: Runner, job: Job, result: dict[str, Any]) -> ConversationMessage:
    msg = s.get(ConversationMessage, job.payload["message_id"])
    conv = s.get(Conversation, job.payload["conversation_id"])
    if msg is None or conv is None:
        raise jobs.ProtocolError(410, "the conversation was deleted")
    answer = ConversationMessage(
        conversation_id=conv.id, user_id=job.user_id, role="assistant", content=result["text"],
        model=result.get("model"), model_used=result.get("model_used"), prompt_tokens=result.get("prompt_tokens"),
        completion_tokens=result.get("completion_tokens"), reasoning_tokens=result.get("reasoning_tokens"),
        duration_s=result.get("duration_s"))
    s.add(answer)
    s.flush()
    conv.updated_at = utcnow()
    jobs.finish(job, {"message_id": answer.id})
    return answer
