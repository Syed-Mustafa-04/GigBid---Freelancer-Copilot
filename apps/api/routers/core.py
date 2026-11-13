"""All HTTP routes.

Kept in one module because they are thin and reading them together is how you
check that tenancy is enforced everywhere - every handler takes ``user`` from
the verified token and scopes its query by ``user.id``.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from agent.config import get_config
from agent.marketplace_port import MarketplaceUnavailable, get_marketplace
from agent.memory.store import get_memory_store
from apps.api import service
from apps.api.auth.security import create_access_token, current_user, hash_password, verify_password
from apps.api.auth.token_store import TokenStore
from apps.api.db import (
    ApprovalRecord,
    MessageRecord,
    Proposal,
    RunTrace,
    SavedSearch,
    Thread,
    User,
    get_db,
)

router = APIRouter()


# ===========================================================================
# Schemas
# ===========================================================================
class RegisterIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8)
    display_name: str = ""
    persona_id: str = "p_ai_engineer"


class LoginIn(BaseModel):
    email: EmailStr
    password: str


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: dict[str, Any]


class SettingsIn(BaseModel):
    persona_id: str | None = None
    autonomy_level: str | None = Field(default=None, pattern="^(manual|assisted|auto_skip)$")
    hourly_rate_usd: float | None = None
    min_project_usd: float | None = None
    weekly_connects_budget: int | None = None
    marketplace_mode: str | None = Field(default=None, pattern="^(gigsim|upwork_live)$")


class SavedSearchIn(BaseModel):
    name: str
    query: str = ""
    skills: list[str] = Field(default_factory=list)
    budget_min: float | None = None
    interval_seconds: int = 3600
    enabled: bool = True


class AnalyseIn(BaseModel):
    job_id: str


class ApprovalIn(BaseModel):
    action: str = Field(pattern="^(approve|edit|reject)$")
    edited_cover_letter: str | None = None
    edited_screening_answers: list[dict[str, Any]] | None = None
    edited_price_usd: float | None = None
    edited_reply_body: str | None = None
    reason: str = ""


class OutcomeIn(BaseModel):
    job_id: str
    proposal_id: str = ""
    state: str = Field(pattern="^(submitted|viewed|replied|interview|hired|lost|ignored)$")
    category: str = ""
    rate_achieved_usd: float | None = None


class MessageRunIn(BaseModel):
    thread_id: str = ""
    job_id: str = ""
    message_id: str = ""
    body: str


def _user_dict(user: User) -> dict[str, Any]:
    return {
        "id": user.id,
        "email": user.email,
        "display_name": user.display_name,
        "persona_id": user.persona_id,
        "role": user.role,
        "autonomy_level": user.autonomy_level,
        "hourly_rate_usd": user.hourly_rate_usd,
        "min_project_usd": user.min_project_usd,
        "weekly_connects_budget": user.weekly_connects_budget,
        "marketplace_mode": user.marketplace_mode,
    }


def _sse(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload, default=str)}\n\n"


# ===========================================================================
# Auth
# ===========================================================================
@router.post("/auth/register", response_model=TokenOut, tags=["auth"])
async def register(payload: RegisterIn, session: Session = Depends(get_db)) -> TokenOut:
    if session.scalar(select(User).where(User.email == payload.email)):
        raise HTTPException(status_code=409, detail="That email is already registered")
    user = User(
        email=payload.email,
        password_hash=hash_password(payload.password),
        display_name=payload.display_name or payload.email.split("@")[0],
        persona_id=payload.persona_id,
    )
    session.add(user)
    session.commit()
    return TokenOut(
        access_token=create_access_token(user.id, email=user.email, role=user.role),
        user=_user_dict(user),
    )


@router.post("/auth/login", response_model=TokenOut, tags=["auth"])
async def login(payload: LoginIn, session: Session = Depends(get_db)) -> TokenOut:
    user = session.scalar(select(User).where(User.email == payload.email))
    if user is None or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Incorrect email or password")
    return TokenOut(
        access_token=create_access_token(user.id, email=user.email, role=user.role),
        user=_user_dict(user),
    )


@router.get("/auth/me", tags=["auth"])
async def me(user: User = Depends(current_user)) -> dict[str, Any]:
    return _user_dict(user)


@router.get("/auth/upwork/status", tags=["auth"])
async def upwork_status(user: User = Depends(current_user)) -> dict[str, Any]:
    return await TokenStore().status(user.id)


@router.post("/auth/upwork/disconnect", tags=["auth"])
async def upwork_disconnect(user: User = Depends(current_user)) -> dict[str, Any]:
    return {"disconnected": await TokenStore().delete(user.id)}


# ===========================================================================
# Settings and memory
# ===========================================================================
@router.get("/settings", tags=["settings"])
async def get_settings(user: User = Depends(current_user), session: Session = Depends(get_db)):
    cfg = get_config()
    searches = session.scalars(select(SavedSearch).where(SavedSearch.user_id == user.id))
    return {
        "user": _user_dict(user),
        "saved_searches": [
            {
                "id": s.id, "name": s.name, "query": s.query, "skills": s.skills,
                "budget_min": s.budget_min, "interval_seconds": s.interval_seconds,
                "enabled": s.enabled,
                "last_run_at": s.last_run_at.isoformat() if s.last_run_at else None,
            }
            for s in searches
        ],
        "budgets": cfg.section("budgets"),
        "thresholds": cfg.section("thresholds"),
        "bidding": cfg.section("bidding"),
        "rag_variant": cfg.get("rag.shipped_variant"),
        "model_profile": cfg.profile_name,
        "marketplace_mode": cfg.marketplace_mode,
    }


@router.patch("/settings", tags=["settings"])
async def update_settings(
    payload: SettingsIn, user: User = Depends(current_user), session: Session = Depends(get_db)
):
    for field, value in payload.model_dump(exclude_none=True).items():
        setattr(user, field, value)
    session.commit()
    return _user_dict(user)


@router.get("/settings/memory", tags=["settings"])
async def get_memory(user: User = Depends(current_user)):
    store = get_memory_store()
    return {"rules": store.all_rules(user.id), "stats": store.stats(user.id)}


@router.delete("/settings/memory/{rule_id}", tags=["settings"])
async def delete_memory(rule_id: str, user: User = Depends(current_user)):
    return {"deleted": get_memory_store().delete_rule(user.id, rule_id)}


@router.delete("/settings/memory", tags=["settings"])
async def clear_memory(
    store: str | None = None, user: User = Depends(current_user)
):
    return {"cleared": get_memory_store().clear(user.id, store)}


@router.post("/settings/searches", tags=["settings"])
async def create_search(
    payload: SavedSearchIn, user: User = Depends(current_user), session: Session = Depends(get_db)
):
    row = SavedSearch(user_id=user.id, **payload.model_dump())
    session.add(row)
    session.commit()
    return {"id": row.id, "name": row.name}


@router.delete("/settings/searches/{search_id}", tags=["settings"])
async def delete_search(
    search_id: str, user: User = Depends(current_user), session: Session = Depends(get_db)
):
    row = session.get(SavedSearch, search_id)
    if row is None or row.user_id != user.id:
        raise HTTPException(status_code=404, detail="Saved search not found")
    session.delete(row)
    session.commit()
    return {"deleted": True}


# ===========================================================================
# Jobs
# ===========================================================================
@router.get("/jobs", tags=["jobs"])
async def list_jobs(
    query: str = "",
    skills: str = "",
    limit: int = Query(default=25, ge=1, le=100),
    user: User = Depends(current_user),
    session: Session = Depends(get_db),
):
    """Job inbox: live marketplace jobs joined with what GigBid already knows."""
    port = get_marketplace()
    degraded = False
    jobs: list[dict[str, Any]] = []
    try:
        jobs = await port.search_jobs(
            query, skills=[s for s in skills.split(",") if s], limit=limit
        )
    except MarketplaceUnavailable:
        degraded = True
    finally:
        await port.aclose()

    threads = {
        t.job_id: t
        for t in session.scalars(select(Thread).where(Thread.user_id == user.id, Thread.kind == "bid"))
    }
    enriched = []
    for job in jobs:
        thread = threads.get(job.get("job_id"))
        enriched.append({
            **job,
            "gigbid": {
                "thread_id": thread.id if thread else None,
                "status": thread.status if thread else "not_analysed",
                "decision": thread.decision if thread else None,
                "fit_score": thread.fit_score if thread else None,
                "risk_score": thread.risk_score if thread else None,
                "price_usd": thread.price_usd if thread else None,
            },
        })
    enriched.sort(key=lambda j: (j["gigbid"]["fit_score"] or -1), reverse=True)
    return {
        "jobs": enriched,
        "count": len(enriched),
        "degraded": degraded,
        "degraded_message": (
            "The marketplace is not responding. These results may be incomplete."
            if degraded else None
        ),
    }


@router.get("/jobs/{job_id}", tags=["jobs"])
async def job_detail(
    job_id: str, user: User = Depends(current_user), session: Session = Depends(get_db)
):
    thread = session.scalar(
        select(Thread).where(Thread.user_id == user.id, Thread.job_id == job_id, Thread.kind == "bid")
    )
    job: dict[str, Any] = (thread.job_snapshot if thread else {}) or {}
    degraded = False
    if not job:
        port = get_marketplace()
        try:
            job = await port.get_job(job_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="Job not found") from None
        except MarketplaceUnavailable:
            degraded = True
        finally:
            await port.aclose()

    proposal = (
        session.scalar(select(Proposal).where(Proposal.thread_id == thread.id)) if thread else None
    )
    trace = (
        session.scalar(
            select(RunTrace).where(RunTrace.thread_id == thread.id).order_by(RunTrace.created_at.desc())
        ) if thread else None
    )
    return {
        "job": job,
        "degraded": degraded,
        "thread": {
            "id": thread.id, "status": thread.status, "decision": thread.decision,
            "fit_score": thread.fit_score, "risk_score": thread.risk_score,
            "price_usd": thread.price_usd, "terminal_reason": thread.terminal_reason,
            "summary": thread.summary,
        } if thread else None,
        "proposal": {
            "id": proposal.id, "cover_letter": proposal.cover_letter,
            "screening_answers": proposal.screening_answers, "claims": proposal.claims,
            "evidence_ids": proposal.evidence_ids, "price_usd": proposal.price_usd,
            "word_count": proposal.word_count, "status": proposal.status,
            "unsupported_before": proposal.unsupported_before,
            "unsupported_after": proposal.unsupported_after,
        } if proposal else None,
        "retrieval": trace.retrieval if trace else {},
        "node_timings": trace.node_timings if trace else {},
    }


@router.post("/jobs/analyse", tags=["jobs"])
async def analyse_job(
    payload: AnalyseIn,
    request: Request,
    user: User = Depends(current_user),
    session: Session = Depends(get_db),
):
    """Run the bid graph, streaming node-by-node progress over SSE."""
    port = get_marketplace()
    try:
        job = await port.get_job(payload.job_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Job not found") from None
    except MarketplaceUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    finally:
        await port.aclose()

    thread_id = await service.start_bid_run(session, user, job)

    async def events():
        try:
            async for event in service.stream_bid_run(session, user, job, thread_id):
                if await request.is_disconnected():
                    break
                yield _sse(event)
        except Exception as exc:  # noqa: BLE001
            yield _sse({"event": "error", "detail": f"{type(exc).__name__}: {exc}"})

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


# ===========================================================================
# Approvals
# ===========================================================================
@router.get("/approvals", tags=["approvals"])
async def list_approvals(user: User = Depends(current_user), session: Session = Depends(get_db)):
    return {"approvals": await service.pending_approvals(session, user)}


@router.get("/approvals/{thread_id}", tags=["approvals"])
async def get_approval(
    thread_id: str, user: User = Depends(current_user), session: Session = Depends(get_db)
):
    record = session.scalar(
        select(ApprovalRecord).where(
            ApprovalRecord.thread_id == thread_id,
            ApprovalRecord.user_id == user.id,
            ApprovalRecord.pending.is_(True),
        )
    )
    if record is None:
        raise HTTPException(status_code=404, detail="No pending approval for this thread")
    return {"approval_id": record.id, "thread_id": thread_id, "kind": record.kind,
            "payload": record.payload}


@router.post("/approvals/{thread_id}", tags=["approvals"])
async def respond_to_approval(
    thread_id: str,
    payload: ApprovalIn,
    user: User = Depends(current_user),
    session: Session = Depends(get_db),
):
    thread = session.get(Thread, thread_id)
    if thread is None or thread.user_id != user.id:
        raise HTTPException(status_code=404, detail="Thread not found")

    response = payload.model_dump(exclude_none=True)
    response["responded_by"] = user.email
    response["responded_at"] = datetime.now(UTC).isoformat()
    return await service.resume_with_approval(
        session, user, thread_id, response, kind=thread.kind
    )


# ===========================================================================
# Proposals / pipeline
# ===========================================================================
@router.get("/proposals", tags=["proposals"])
async def list_proposals(user: User = Depends(current_user), session: Session = Depends(get_db)):
    rows = session.scalars(
        select(Proposal).where(Proposal.user_id == user.id).order_by(Proposal.updated_at.desc())
    )
    out = []
    for p in rows:
        thread = session.get(Thread, p.thread_id)
        out.append({
            "id": p.id, "thread_id": p.thread_id, "job_id": p.job_id,
            "job_title": ((thread.job_snapshot if thread else {}) or {}).get("title", ""),
            "status": p.status, "price_usd": p.price_usd, "word_count": p.word_count,
            "connects_spent": p.connects_spent,
            "unsupported_after": p.unsupported_after,
            "updated_at": p.updated_at.isoformat(),
        })
    return {"proposals": out, "count": len(out)}


@router.post("/proposals/outcome", tags=["proposals"])
async def submit_outcome(
    payload: OutcomeIn, user: User = Depends(current_user), session: Session = Depends(get_db)
):
    return service.record_outcome(
        session, user, job_id=payload.job_id, proposal_id=payload.proposal_id,
        state=payload.state, category=payload.category,
        rate_achieved_usd=payload.rate_achieved_usd,
    )


@router.post("/proposals/sync", tags=["proposals"])
async def sync_outcomes(user: User = Depends(current_user), session: Session = Depends(get_db)):
    """Pull current proposal states from the marketplace into the pipeline board."""
    port = get_marketplace()
    updated = 0
    try:
        from mcp_servers.marketplace.adapters.gigsim import GigSimAdapter

        if isinstance(port, GigSimAdapter):
            data = await port._request("GET", "/proposals")
            remote = {p["proposal_id"]: p for p in data.get("proposals", [])}
            for proposal in session.scalars(select(Proposal).where(Proposal.user_id == user.id)):
                row = remote.get(proposal.marketplace_proposal_id)
                if row and row.get("status") and row["status"] != proposal.status:
                    proposal.status = row["status"]
                    updated += 1
                    thread = session.get(Thread, proposal.thread_id)
                    skills = ((thread.job_snapshot if thread else {}) or {}).get("required_skills") or ["general"]
                    service.record_outcome(
                        session, user, job_id=proposal.job_id,
                        proposal_id=proposal.marketplace_proposal_id,
                        state=row["status"], category=skills[0],
                    )
            session.commit()
    except MarketplaceUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    finally:
        await port.aclose()
    return {"updated": updated}


# ===========================================================================
# Messages
# ===========================================================================
@router.get("/messages", tags=["messages"])
async def list_messages(user: User = Depends(current_user), session: Session = Depends(get_db)):
    rows = session.scalars(
        select(MessageRecord).where(MessageRecord.user_id == user.id)
        .order_by(MessageRecord.created_at.desc()).limit(200)
    )
    threads: dict[str, list[dict[str, Any]]] = {}
    for m in rows:
        threads.setdefault(m.thread_id, []).append({
            "id": m.id, "sender": m.sender, "body": m.body, "intent": m.intent,
            "flagged": m.flagged, "created_at": m.created_at.isoformat(),
        })
    return {"threads": threads, "count": len(threads)}


@router.post("/messages/handle", tags=["messages"])
async def handle_message(
    payload: MessageRunIn,
    request: Request,
    user: User = Depends(current_user),
    session: Session = Depends(get_db),
):
    thread_id = payload.thread_id or service.new_thread_id("c")
    message = {
        "message_id": payload.message_id or uuid.uuid4().hex[:12],
        "thread_id": thread_id,
        "job_id": payload.job_id,
        "sender": "client",
        "body": payload.body,
        "created_at": datetime.now(UTC).isoformat(),
    }

    async def events():
        try:
            async for event in service.stream_conversation_run(session, user, message, thread_id):
                if await request.is_disconnected():
                    break
                yield _sse(event)
        except Exception as exc:  # noqa: BLE001
            yield _sse({"event": "error", "detail": f"{type(exc).__name__}: {exc}"})

    return StreamingResponse(
        events(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/messages/poll", tags=["messages"])
async def poll_messages(user: User = Depends(current_user), session: Session = Depends(get_db)):
    """Fetch new client messages from the marketplace."""
    port = get_marketplace()
    try:
        messages = await port.list_messages()
    except MarketplaceUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    finally:
        await port.aclose()

    known = {
        m.marketplace_message_id
        for m in session.scalars(select(MessageRecord).where(MessageRecord.user_id == user.id))
    }
    new = [m for m in messages if m.get("sender") == "client" and m.get("message_id") not in known]
    for m in new:
        session.add(MessageRecord(
            user_id=user.id, thread_id=m.get("thread_id", ""), job_id=m.get("job_id", ""),
            marketplace_message_id=m.get("message_id", ""), sender="client", body=m.get("body", ""),
        ))
    session.commit()
    return {"new_messages": new, "count": len(new)}


# ===========================================================================
# Runs, traces, analytics
# ===========================================================================
@router.get("/runs", tags=["runs"])
async def list_runs(user: User = Depends(current_user), session: Session = Depends(get_db)):
    rows = session.scalars(
        select(Thread).where(Thread.user_id == user.id).order_by(Thread.created_at.desc()).limit(100)
    )
    return {
        "runs": [
            {
                "thread_id": t.id, "kind": t.kind, "job_id": t.job_id,
                "job_title": (t.job_snapshot or {}).get("title", ""),
                "status": t.status, "decision": t.decision,
                "fit_score": t.fit_score, "risk_score": t.risk_score,
                "tokens_used": t.tokens_used, "cost_usd": t.cost_usd,
                "latency_s": t.latency_s, "degraded": t.degraded,
                "terminal_reason": t.terminal_reason,
                "created_at": t.created_at.isoformat(),
            }
            for t in rows
        ]
    }


@router.get("/runs/{thread_id}", tags=["runs"])
async def run_detail(
    thread_id: str, user: User = Depends(current_user), session: Session = Depends(get_db)
):
    thread = session.get(Thread, thread_id)
    if thread is None or thread.user_id != user.id:
        raise HTTPException(status_code=404, detail="Run not found")
    traces = session.scalars(
        select(RunTrace).where(RunTrace.thread_id == thread_id).order_by(RunTrace.created_at)
    )
    merged: list[dict[str, Any]] = []
    timings: dict[str, Any] = {}
    retrieval: dict[str, Any] = {}
    for t in traces:
        merged.extend(t.trace or [])
        timings.update(t.node_timings or {})
        retrieval.update(t.retrieval or {})
    return {
        "thread_id": thread_id,
        "status": thread.status,
        "job_id": thread.job_id,
        "job_title": (thread.job_snapshot or {}).get("title", ""),
        "summary": thread.summary,
        "terminal_reason": thread.terminal_reason,
        "trace": merged,
        "node_timings": timings,
        "retrieval": retrieval,
        "totals": {
            "tokens": thread.tokens_used, "cost_usd": thread.cost_usd,
            "latency_s": thread.latency_s,
        },
    }


@router.get("/analytics", tags=["analytics"])
async def get_analytics(user: User = Depends(current_user), session: Session = Depends(get_db)):
    return service.analytics(session, user)


# ===========================================================================
# System
# ===========================================================================
@router.get("/system/status", tags=["system"])
async def system_status() -> dict[str, Any]:
    """Drives the degraded-mode banner. Must never itself fail."""
    cfg = get_config()
    from agent.llm import get_llm_client

    marketplace: dict[str, Any] = {"available": False}
    port = get_marketplace()
    try:
        marketplace = await asyncio.wait_for(port.health(), timeout=5.0)
    except Exception as exc:  # noqa: BLE001
        marketplace = {"available": False, "error": str(exc)[:200]}
    finally:
        await port.aclose()

    client = get_llm_client()
    return {
        "marketplace": {
            "mode": cfg.marketplace_mode,
            "adapter": port.name,
            "available": bool(marketplace.get("available")),
            "detail": marketplace,
        },
        "llm": {
            "profile": cfg.profile_name,
            "offline_stub": client.is_offline,
            "warning": (
                "No LLM API key is configured. GigBid is running on the deterministic "
                "offline provider: the graph executes but the text and scores it produces "
                "are placeholders, not model output."
            ) if client.is_offline else None,
        },
        "rag": {
            "shipped_variant": cfg.get("rag.shipped_variant"),
            "embedding_backend": os.getenv("EMBEDDING_BACKEND", "local"),
        },
        "budgets": cfg.section("budgets"),
    }
