"""SQLAlchemy mapping of the tables. The schema itself (with row-level security) comes from
migrations/; keep both in step."""
from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import BigInteger, Boolean, Date, DateTime, Float, ForeignKey, Integer, Text, func
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
    translate_auto: Mapped[bool] = mapped_column(Boolean, default=True)
    quota_storage_mb: Mapped[int | None] = mapped_column(Integer)
    quota_upload_mb: Mapped[int | None] = mapped_column(Integer)
    totp_after_google: Mapped[bool] = mapped_column(Boolean, default=False)
    totp_pending_secret: Mapped[str | None] = mapped_column(Text)


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
    purpose: Mapped[str] = mapped_column(Text, default="login")  # login | youtube
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
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


class Translation(Base):
    __tablename__ = "translations"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    transcript_id: Mapped[int] = mapped_column(ForeignKey("transcripts.id", ondelete="CASCADE"))
    recipe_hash: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(Text)
    language: Mapped[str] = mapped_column(Text)
    source_language: Mapped[str | None] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(Text)
    model_used: Mapped[str | None] = mapped_column(Text)
    prompt_version: Mapped[int] = mapped_column(Integer)
    segment_count: Mapped[int] = mapped_column(Integer, default=0)
    missing: Mapped[int] = mapped_column(Integer, default=0)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer)
    completion_tokens: Mapped[int | None] = mapped_column(Integer)
    reasoning_tokens: Mapped[int | None] = mapped_column(Integer)
    duration_s: Mapped[float | None] = mapped_column(Float)
    private: Mapped[bool] = mapped_column(Boolean, default=False)
    shareable: Mapped[bool] = mapped_column(Boolean, default=True)
    origin_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    runner_version: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _now()
    segments: Mapped[list[TranslationSegment]] = relationship(
        order_by="TranslationSegment.idx", lazy="selectin", cascade="all, delete-orphan", passive_deletes=True)


class TranslationSegment(Base):
    __tablename__ = "translation_segments"
    translation_id: Mapped[int] = mapped_column(ForeignKey("translations.id", ondelete="CASCADE"), primary_key=True)
    idx: Mapped[int] = mapped_column(Integer, primary_key=True)
    text: Mapped[str] = mapped_column(Text)


class Item(Base):
    __tablename__ = "items"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"))
    title: Mapped[str | None] = mapped_column(Text)
    is_public: Mapped[bool] = mapped_column(Boolean)
    main_transcript_id: Mapped[int | None] = mapped_column(ForeignKey("transcripts.id", ondelete="SET NULL"))
    current_summary_id: Mapped[int | None] = mapped_column(ForeignKey("summaries.id", ondelete="SET NULL"))
    current_translation_id: Mapped[int | None] = mapped_column(ForeignKey("translations.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = _now()
    updated_at: Mapped[datetime] = _now()
    notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    note: Mapped[str | None] = mapped_column(Text)
    source: Mapped[Source] = relationship(lazy="joined")


class ItemTranscript(Base):
    __tablename__ = "item_transcripts"
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id", ondelete="CASCADE"), primary_key=True)
    transcript_id: Mapped[int] = mapped_column(ForeignKey("transcripts.id", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    reused: Mapped[bool] = mapped_column(Boolean, default=False)
    added_at: Mapped[datetime] = _now()
    speaker_names: Mapped[dict[str, Any]] = mapped_column(default=dict)


class ItemTag(Base):
    __tablename__ = "item_tags"
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id", ondelete="CASCADE"), primary_key=True)
    tag: Mapped[str] = mapped_column(Text, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))


class ItemSummary(Base):
    __tablename__ = "item_summaries"
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id", ondelete="CASCADE"), primary_key=True)
    summary_id: Mapped[int] = mapped_column(ForeignKey("summaries.id", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    reused: Mapped[bool] = mapped_column(Boolean, default=False)
    added_at: Mapped[datetime] = _now()


class ItemTranslation(Base):
    __tablename__ = "item_translations"
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id", ondelete="CASCADE"), primary_key=True)
    translation_id: Mapped[int] = mapped_column(ForeignKey("translations.id", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    reused: Mapped[bool] = mapped_column(Boolean, default=False)
    added_at: Mapped[datetime] = _now()


class IndexQueue(Base):
    __tablename__ = "index_queue"
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    queued_at: Mapped[datetime] = _now()
    run_after: Mapped[datetime] = _now()
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)


class Upload(Base):
    __tablename__ = "uploads"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    sha256: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    size: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = _now()
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


JOB_STATES = ("queued", "running", "done", "failed", "cancelled")
ACTIVE = ("queued", "running")


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    item_id: Mapped[int | None] = mapped_column(ForeignKey("items.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(Text)  # transcribe | summarize | translate
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


# ---- YouTube subscriptions (round 4) --------------------------------------------------------

class Channel(Base):
    __tablename__ = "channels"
    id: Mapped[str] = mapped_column(Text, primary_key=True)
    title: Mapped[str | None] = mapped_column(Text)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _now()


class ChannelVideo(Base):
    __tablename__ = "channel_videos"
    video_id: Mapped[str] = mapped_column(Text, primary_key=True)
    channel_id: Mapped[str] = mapped_column(ForeignKey("channels.id", ondelete="CASCADE"))
    title: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_short: Mapped[bool | None] = mapped_column(Boolean)
    seen_at: Mapped[datetime] = _now()


class Subscription(Base):
    __tablename__ = "subscriptions"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    channel_id: Mapped[str] = mapped_column(ForeignKey("channels.id"))
    source: Mapped[str] = mapped_column(Text)  # google | takeout | manual
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    rule: Mapped[str] = mapped_column(Text, default="ask")  # always | ask | never
    allow_cloud: Mapped[bool] = mapped_column(Boolean, default=False)
    notify: Mapped[str] = mapped_column(Text, default="digest")  # digest | instant | none
    baseline_at: Mapped[datetime] = _now()
    created_at: Mapped[datetime] = _now()
    channel: Mapped[Channel] = relationship(lazy="selectin")


INBOX_STATES = ("new", "queued", "done", "skipped", "failed", "short")


class InboxEntry(Base):
    __tablename__ = "inbox_entries"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    channel_id: Mapped[str] = mapped_column(ForeignKey("channels.id"))
    video_id: Mapped[str] = mapped_column(Text)
    title: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    state: Mapped[str] = mapped_column(Text, default="new")
    item_id: Mapped[int | None] = mapped_column(ForeignKey("items.id", ondelete="SET NULL"))
    job_id: Mapped[int | None] = mapped_column(ForeignKey("jobs.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = _now()
    updated_at: Mapped[datetime] = _now()
    channel: Mapped[Channel] = relationship(lazy="selectin")


class YoutubeLink(Base):
    __tablename__ = "youtube_links"
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    email: Mapped[str | None] = mapped_column(Text)
    refresh_token_enc: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = _now()
    synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sync_error: Mapped[str | None] = mapped_column(Text)
    sync_count: Mapped[int | None] = mapped_column(Integer)
    sync_total: Mapped[int | None] = mapped_column(Integer)  # what YouTube reported (pageInfo.totalResults)
    channel_title: Mapped[str | None] = mapped_column(Text)  # the YouTube channel the consent belongs to


# ---- notifications (round 4b) ---------------------------------------------------------------

class NotifySettings(Base):
    __tablename__ = "notify_settings"
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    channel: Mapped[str] = mapped_column(Text, default="none")  # none | ntfy | ha
    ntfy_server: Mapped[str | None] = mapped_column(Text)
    ntfy_topic: Mapped[str | None] = mapped_column(Text)
    ntfy_token_enc: Mapped[str | None] = mapped_column(Text)
    ha_url: Mapped[str | None] = mapped_column(Text)
    ha_token_enc: Mapped[str | None] = mapped_column(Text)
    ha_service: Mapped[str | None] = mapped_column(Text)
    digest_time: Mapped[str] = mapped_column(Text, default="19:00")
    on_manual: Mapped[bool] = mapped_column(Boolean, default=True)
    on_error: Mapped[bool] = mapped_column(Boolean, default=True)
    last_digest_on: Mapped[date | None] = mapped_column(Date)
    updated_at: Mapped[datetime] = _now()


class Notification(Base):
    __tablename__ = "notifications"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(Text)  # done | error | digest | test
    digest: Mapped[bool] = mapped_column(Boolean, default=False)
    item_id: Mapped[int | None] = mapped_column(ForeignKey("items.id", ondelete="SET NULL"))
    title: Mapped[str] = mapped_column(Text)
    message: Mapped[str] = mapped_column(Text)
    url: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _now()
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)


class McpToken(Base):
    __tablename__ = "mcp_tokens"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(Text)
    token_hash: Mapped[str] = mapped_column(Text, unique=True)
    can_start: Mapped[bool] = mapped_column(Boolean, default=False)
    include_private: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = _now()
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
