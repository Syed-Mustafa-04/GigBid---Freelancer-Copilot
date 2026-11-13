"""Persistence for the application layer.

Separate from the LangGraph checkpointer on purpose. The checkpointer owns
*graph execution* - what node runs next, what the interrupt is waiting for.
This owns *product state* - users, jobs seen, proposals, approvals, the
pipeline board, outcomes. Conflating them makes it impossible to change the
graph without migrating business data.

Every query is scoped by ``user_id``. Tenancy is enforced at the query, not by
filtering results afterwards.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    create_engine,
    select,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    relationship,
    sessionmaker,
)

from agent.config import Secrets


def _uuid() -> str:
    return uuid.uuid4().hex


def _now() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    display_name: Mapped[str] = mapped_column(String(120), default="")
    persona_id: Mapped[str] = mapped_column(String(64), default="p_ai_engineer")
    role: Mapped[str] = mapped_column(String(32), default="freelancer")  # freelancer | reviewer
    autonomy_level: Mapped[str] = mapped_column(String(32), default="assisted")
    hourly_rate_usd: Mapped[float] = mapped_column(Float, default=60.0)
    min_project_usd: Mapped[float] = mapped_column(Float, default=500.0)
    weekly_connects_budget: Mapped[int] = mapped_column(Integer, default=120)
    marketplace_mode: Mapped[str] = mapped_column(String(32), default="gigsim")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    threads: Mapped[list[Thread]] = relationship(back_populates="user", cascade="all, delete-orphan")
    searches: Mapped[list[SavedSearch]] = relationship(back_populates="user", cascade="all, delete-orphan")


class SavedSearch(Base):
    __tablename__ = "saved_searches"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    query: Mapped[str] = mapped_column(String(500), default="")
    skills: Mapped[list] = mapped_column(JSON, default=list)
    budget_min: Mapped[float | None] = mapped_column(Float, nullable=True)
    interval_seconds: Mapped[int] = mapped_column(Integer, default=3600)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    user: Mapped[User] = relationship(back_populates="searches")


class Thread(Base):
    """One graph thread: a job being assessed, or a conversation."""

    __tablename__ = "threads"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    kind: Mapped[str] = mapped_column(String(32), default="bid")  # bid | conversation
    job_id: Mapped[str] = mapped_column(String(64), index=True, default="")
    status: Mapped[str] = mapped_column(String(32), default="new", index=True)
    decision: Mapped[str] = mapped_column(String(16), default="")
    fit_score: Mapped[float] = mapped_column(Float, default=0.0)
    risk_score: Mapped[float] = mapped_column(Float, default=0.0)
    price_usd: Mapped[float] = mapped_column(Float, default=0.0)
    connects_cost: Mapped[int] = mapped_column(Integer, default=0)
    tokens_used: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    latency_s: Mapped[float] = mapped_column(Float, default=0.0)
    job_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    summary: Mapped[dict] = mapped_column(JSON, default=dict)
    terminal_reason: Mapped[str] = mapped_column(Text, default="")
    degraded: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)

    user: Mapped[User] = relationship(back_populates="threads")
    proposals: Mapped[list[Proposal]] = relationship(back_populates="thread", cascade="all, delete-orphan")


class Proposal(Base):
    __tablename__ = "proposals"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    thread_id: Mapped[str] = mapped_column(ForeignKey("threads.id"), index=True)
    user_id: Mapped[str] = mapped_column(String(32), index=True)
    job_id: Mapped[str] = mapped_column(String(64), index=True)
    marketplace_proposal_id: Mapped[str] = mapped_column(String(64), default="")
    cover_letter: Mapped[str] = mapped_column(Text, default="")
    screening_answers: Mapped[list] = mapped_column(JSON, default=list)
    evidence_ids: Mapped[list] = mapped_column(JSON, default=list)
    claims: Mapped[list] = mapped_column(JSON, default=list)
    price_usd: Mapped[float] = mapped_column(Float, default=0.0)
    word_count: Mapped[int] = mapped_column(Integer, default=0)
    revision: Mapped[int] = mapped_column(Integer, default=0)
    unsupported_before: Mapped[int] = mapped_column(Integer, default=0)
    unsupported_after: Mapped[int] = mapped_column(Integer, default=0)
    # drafted | pending_approval | approved | rejected | submitted | viewed |
    # replied | interview | hired | lost | ignored
    status: Mapped[str] = mapped_column(String(32), default="drafted", index=True)
    connects_spent: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)

    thread: Mapped[Thread] = relationship(back_populates="proposals")


class ApprovalRecord(Base):
    __tablename__ = "approvals"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    thread_id: Mapped[str] = mapped_column(String(64), index=True)
    user_id: Mapped[str] = mapped_column(String(32), index=True)
    kind: Mapped[str] = mapped_column(String(32), default="proposal")
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    action: Mapped[str] = mapped_column(String(16), default="")
    reason: Mapped[str] = mapped_column(Text, default="")
    text_before: Mapped[str] = mapped_column(Text, default="")
    text_after: Mapped[str] = mapped_column(Text, default="")
    responded_by: Mapped[str] = mapped_column(String(120), default="")
    pending: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)
    responded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class MessageRecord(Base):
    __tablename__ = "messages"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(String(32), index=True)
    thread_id: Mapped[str] = mapped_column(String(64), index=True)
    job_id: Mapped[str] = mapped_column(String(64), default="")
    marketplace_message_id: Mapped[str] = mapped_column(String(64), default="")
    sender: Mapped[str] = mapped_column(String(16), default="client")
    body: Mapped[str] = mapped_column(Text, default="")
    intent: Mapped[str] = mapped_column(String(32), default="")
    flagged: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)


class OutcomeRecord(Base):
    __tablename__ = "outcomes"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(String(32), index=True)
    job_id: Mapped[str] = mapped_column(String(64), index=True)
    proposal_id: Mapped[str] = mapped_column(String(64), default="")
    category: Mapped[str] = mapped_column(String(120), default="")
    state: Mapped[str] = mapped_column(String(32), default="")
    rate_achieved_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class OAuthToken(Base):
    """Encrypted at rest. Never logged, never returned by any endpoint."""

    __tablename__ = "oauth_tokens"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(String(32), index=True)
    provider: Mapped[str] = mapped_column(String(32), default="upwork")
    access_token_enc: Mapped[str] = mapped_column(Text)
    refresh_token_enc: Mapped[str] = mapped_column(Text, default="")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    scopes: Mapped[str] = mapped_column(String(500), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class AuditLog(Base):
    """Append-only. PII masked before write."""

    __tablename__ = "audit_log"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(String(32), index=True)
    tool: Mapped[str] = mapped_column(String(64))
    server: Mapped[str] = mapped_column(String(32), default="")
    arguments: Mapped[str] = mapped_column(Text, default="")
    result: Mapped[str] = mapped_column(Text, default="")
    trace_id: Mapped[str] = mapped_column(String(64), default="")
    thread_id: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)


class RunTrace(Base):
    """One stored execution trace, for the run viewer and trace links."""

    __tablename__ = "run_traces"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    thread_id: Mapped[str] = mapped_column(String(64), index=True)
    user_id: Mapped[str] = mapped_column(String(32), index=True)
    trace: Mapped[list] = mapped_column(JSON, default=list)
    node_timings: Mapped[dict] = mapped_column(JSON, default=dict)
    retrieval: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)


# ---------------------------------------------------------------------------
# Engine / session
# ---------------------------------------------------------------------------
_ENGINE = None
_SESSION_FACTORY = None


def get_engine():
    global _ENGINE
    if _ENGINE is None:
        url = Secrets.database_url()
        kwargs: dict[str, Any] = {"future": True, "pool_pre_ping": True}
        if url.startswith("sqlite"):
            kwargs["connect_args"] = {"check_same_thread": False}
        _ENGINE = create_engine(url, **kwargs)
    return _ENGINE


def get_session_factory():
    global _SESSION_FACTORY
    if _SESSION_FACTORY is None:
        _SESSION_FACTORY = sessionmaker(bind=get_engine(), expire_on_commit=False, future=True)
    return _SESSION_FACTORY


def init_db() -> None:
    Base.metadata.create_all(get_engine())


def get_db():
    """FastAPI dependency."""
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.close()


def reset_engine() -> None:
    global _ENGINE, _SESSION_FACTORY
    _ENGINE = None
    _SESSION_FACTORY = None


# ---------------------------------------------------------------------------
# Helpers used by routers
# ---------------------------------------------------------------------------
def record_audit(
    session: Session, *, user_id: str, tool: str, server: str = "",
    arguments: Any = None, result: Any = None, trace_id: str = "", thread_id: str = "",
) -> None:
    from agent.safety import mask_pii

    session.add(
        AuditLog(
            user_id=user_id,
            tool=tool,
            server=server,
            arguments=mask_pii(json.dumps(arguments, default=str))[:4000] if arguments else "",
            result=mask_pii(json.dumps(result, default=str))[:2000] if result else "",
            trace_id=trace_id,
            thread_id=thread_id,
        )
    )
    session.commit()


def user_threads(session: Session, user_id: str, *, kind: str = "bid", limit: int = 100):
    return list(
        session.scalars(
            select(Thread)
            .where(Thread.user_id == user_id, Thread.kind == kind)
            .order_by(Thread.created_at.desc())
            .limit(limit)
        )
    )
