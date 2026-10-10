"""SQLAlchemy mapping of the tables. The schema itself (with row-level security) comes from
migrations/; keep both in step."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, Boolean, DateTime, Float, ForeignKey, Integer, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _now() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now())


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSONB, list[Any]: JSONB}


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    email: Mapped[str] = mapped_column(Text)
    google_sub: Mapped[str | None] = mapped_column(Text, unique=True)
    name: Mapped[str | None] = mapped_column(Text)
    role: Mapped[str] = mapped_column(Text, default="user")
    password_hash: Mapped[str | None] = mapped_column(Text)
    totp_secret: Mapped[str | None] = mapped_column(Text)
    totp_last_step: Mapped[int] = mapped_column(BigInteger, default=0)
    failed_logins: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    disabled: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = _now()
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Invite(Base):
    __tablename__ = "invites"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    email: Mapped[str] = mapped_column(Text)
    token_hash: Mapped[str] = mapped_column(Text, unique=True)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = _now()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    used_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Session(Base):
    __tablename__ = "sessions"
    token_hash: Mapped[str] = mapped_column(Text, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    state: Mapped[str] = mapped_column(Text)  # pending (password ok, waiting for the code) | active
    csrf_token: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = _now()
    last_seen_at: Mapped[datetime] = _now()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ip: Mapped[str | None] = mapped_column(Text)
    user_agent: Mapped[str | None] = mapped_column(Text)


class LoginState(Base):
    __tablename__ = "login_states"
    state_hash: Mapped[str] = mapped_column(Text, primary_key=True)
    nonce: Mapped[str] = mapped_column(Text)
    verifier: Mapped[str] = mapped_column(Text)
    invite_token_hash: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _now()


class Runner(Base):
    __tablename__ = "runners"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(Text)
    token_hash: Mapped[str] = mapped_column(Text, unique=True)
    created_at: Mapped[datetime] = _now()
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[str | None] = mapped_column(Text)
    protocol: Mapped[int | None] = mapped_column(Integer)
    capabilities: Mapped[dict[str, Any]] = mapped_column(default=dict)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PairRequest(Base):
    __tablename__ = "pair_requests"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    code: Mapped[str] = mapped_column(Text, unique=True)
    poll_hash: Mapped[str] = mapped_column(Text, unique=True)
    created_at: Mapped[datetime] = _now()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    runner_name: Mapped[str | None] = mapped_column(Text)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Source(Base):
    __tablename__ = "sources"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    kind: Mapped[str] = mapped_column(Text)  # youtube | url | upload
    key: Mapped[str] = mapped_column(Text, unique=True)
    url: Mapped[str | None] = mapped_column(Text)
    meta: Mapped[dict[str, Any]] = mapped_column(default=dict)
    created_at: Mapped[datetime] = _now()
    updated_at: Mapped[datetime] = _now()


class Transcript(Base):
    __tablename__ = "transcripts"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"))
    recipe_hash: Mapped[str] = mapped_column(Text)
    mode: Mapped[str] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(Text)
    caption_kind: Mapped[str | None] = mapped_column(Text)
    language: Mapped[str | None] = mapped_column(Text)
    diarized: Mapped[bool] = mapped_column(Boolean, default=False)
    speakers: Mapped[list[Any]] = mapped_column(default=list)
    warnings: Mapped[list[Any]] = mapped_column(default=list)
    cloud: Mapped[dict[str, Any] | None] = mapped_column()
    options: Mapped[dict[str, Any]] = mapped_column(default=dict)
    private: Mapped[bool] = mapped_column(Boolean, default=False)
    shareable: Mapped[bool] = mapped_column(Boolean, default=True)
    origin_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    runner_version: Mapped[str | None] = mapped_column(Text)
    elapsed_s: Mapped[float | None] = mapped_column(Float)
    segment_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = _now()
    segments: Mapped[list["Segment"]] = relationship(order_by="Segment.idx", lazy="selectin",
                                                     cascade="all, delete-orphan", passive_deletes=True)


class Segment(Base):
    __tablename__ = "segments"
    transcript_id: Mapped[int] = mapped_column(ForeignKey("transcripts.id", ondelete="CASCADE"), primary_key=True)
    idx: Mapped[int] = mapped_column(Integer, primary_key=True)
    start_s: Mapped[float] = mapped_column(Float)
    end_s: Mapped[float] = mapped_column(Float)
    speaker: Mapped[str | None] = mapped_column(Text)
    text: Mapped[str] = mapped_column(Text)


class Summary(Base):
    __tablename__ = "summaries"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    transcript_id: Mapped[int] = mapped_column(ForeignKey("transcripts.id", ondelete="CASCADE"))
    recipe_hash: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(Text)  # public | private: which model the runner used
    model: Mapped[str | None] = mapped_column(Text)
    model_used: Mapped[str | None] = mapped_column(Text)
    language: Mapped[str] = mapped_column(Text)
    prompt_version: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    chunked: Mapped[bool] = mapped_column(Boolean, default=False)
    input_chars: Mapped[int] = mapped_column(Integer, default=0)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer)
    completion_tokens: Mapped[int | None] = mapped_column(Integer)
    reasoning_tokens: Mapped[int | None] = mapped_column(Integer)
    duration_s: Mapped[float | None] = mapped_column(Float)
    private: Mapped[bool] = mapped_column(Boolean, default=False)
    shareable: Mapped[bool] = mapped_column(Boolean, default=True)
    origin_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    runner_version: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _now()


class Item(Base):
    __tablename__ = "items"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"))
    title: Mapped[str | None] = mapped_column(Text)
    is_public: Mapped[bool] = mapped_column(Boolean)
    main_transcript_id: Mapped[int | None] = mapped_column(ForeignKey("transcripts.id", ondelete="SET NULL"))
    current_summary_id: Mapped[int | None] = mapped_column(ForeignKey("summaries.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = _now()
    updated_at: Mapped[datetime] = _now()
    source: Mapped[Source] = relationship(lazy="joined")


class ItemTranscript(Base):
    __tablename__ = "item_transcripts"
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id", ondelete="CASCADE"), primary_key=True)
    transcript_id: Mapped[int] = mapped_column(ForeignKey("transcripts.id", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    reused: Mapped[bool] = mapped_column(Boolean, default=False)
    added_at: Mapped[datetime] = _now()


class ItemSummary(Base):
    __tablename__ = "item_summaries"
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id", ondelete="CASCADE"), primary_key=True)
    summary_id: Mapped[int] = mapped_column(ForeignKey("summaries.id", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    reused: Mapped[bool] = mapped_column(Boolean, default=False)
    added_at: Mapped[datetime] = _now()


JOB_STATES = ("queued", "running", "done", "failed", "cancelled")
ACTIVE = ("queued", "running")


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    item_id: Mapped[int | None] = mapped_column(ForeignKey("items.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(Text)  # transcribe | summarize
    state: Mapped[str] = mapped_column(Text, default="queued")
    title: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(default=dict)
    runner_id: Mapped[int | None] = mapped_column(ForeignKey("runners.id", ondelete="SET NULL"))
    stage: Mapped[str | None] = mapped_column(Text)
    progress: Mapped[dict[str, Any]] = mapped_column(default=dict)
    error: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    run_after: Mapped[datetime] = _now()
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = _now()
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = _now()
