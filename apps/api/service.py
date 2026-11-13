"""Orchestration between HTTP, the graphs, and product persistence.

Routers stay thin: they authenticate, validate and call into here. This module
owns the sequence that is easy to get subtly wrong - start a thread, stream
node updates, persist what the UI needs, resume an interrupt with the user's
decision, and feed confirmed outcomes back into memory.

One rule throughout: the graph is the source of truth for *execution*, the
database is the source of truth for *what the user sees*. When they disagree,
the graph wins and the database is corrected from it.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from langgraph.types import Command
from sqlalchemy import select
from sqlalchemy.orm import Session

from agent.handoffs import Outcome
from agent.memory.store import get_memory_store
from agent.state import new_bid_state, new_conversation_state, summarise
from apps.api.db import (
    ApprovalRecord,
    MessageRecord,
    OutcomeRecord,
    Proposal,
    RunTrace,
    Thread,
    User,
)


def _now() -> datetime:
    return datetime.now(UTC)


def new_thread_id(prefix: str = "t") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


# ---------------------------------------------------------------------------
# Graph access
# ---------------------------------------------------------------------------
async def bid_graph():
    from agent.graphs.bid_graph import get_bid_graph

    return await get_bid_graph()


async def conversation_graph():
    from agent.graphs.conversation_graph import get_conversation_graph

    return await get_conversation_graph()


def thread_config(thread_id: str, *, user_id: str) -> dict[str, Any]:
    return {
        "configurable": {"thread_id": thread_id, "user_id": user_id},
        "recursion_limit": 80,
    }


# ---------------------------------------------------------------------------
# Running a job
# ---------------------------------------------------------------------------
async def start_bid_run(
    session: Session, user: User, job: dict[str, Any], *, thread_id: str | None = None
) -> str:
    """Create the thread row before the graph runs, so the UI can show it at once."""
    tid = thread_id or new_thread_id()
    row = Thread(
        id=tid,
        user_id=user.id,
        kind="bid",
        job_id=job.get("job_id", ""),
        status="new",
        job_snapshot=job,
        connects_cost=int(job.get("connects_cost") or 0),
    )
    session.merge(row)
    session.commit()
    return tid


def _memory_for(user: User, job: dict[str, Any]) -> dict[str, Any]:
    store = get_memory_store()
    category = (job.get("required_skills") or ["general"])[0]
    return {
        "style_rules": store.style_rules_text(user.id),
        "outcome_rules": store.outcome_rules_text(user.id, category=category),
    }


async def stream_bid_run(
    session: Session, user: User, job: dict[str, Any], thread_id: str
) -> AsyncIterator[dict[str, Any]]:
    """Run the graph, yielding one event per node so the UI never shows a spinner."""
    graph = await bid_graph()
    config = thread_config(thread_id, user_id=user.id)
    state = new_bid_state(
        job, user_id=user.id, persona_id=user.persona_id, thread_id=thread_id
    )
    state.update(_memory_for(user, job))

    started = time.perf_counter()
    final: dict[str, Any] = {}

    yield {"event": "run_started", "thread_id": thread_id, "job_id": job.get("job_id", "")}

    async for chunk in graph.astream(state, config, stream_mode="updates"):
        for node_name, update in chunk.items():
            if node_name == "__interrupt__":
                payload = update[0].value if update else {}
                _persist_approval(session, user, thread_id, payload)
                yield {
                    "event": "approval_required",
                    "thread_id": thread_id,
                    "payload": payload.get("approval_request", {}),
                }
                continue
            final.update(update or {})
            yield {
                "event": "node_complete",
                "thread_id": thread_id,
                "node": node_name,
                "status": (update or {}).get("status"),
                "summary": _node_summary(node_name, update or {}),
            }

    snapshot = await graph.aget_state(config)
    values = snapshot.values or final
    _persist_thread(session, user, thread_id, values, latency_s=time.perf_counter() - started)
    yield {
        "event": "run_complete",
        "thread_id": thread_id,
        "summary": summarise(values),
        "awaiting_approval": bool(snapshot.interrupts),
    }


def _node_summary(node: str, update: dict[str, Any]) -> dict[str, Any]:
    """Small, UI-shaped summary. Never the whole state."""
    out: dict[str, Any] = {}
    if (spec := update.get("job_spec")) is not None:
        out["requirements"] = len(spec.requirements)
        out["complexity"] = spec.complexity
    if (risk := update.get("risk_report")) is not None:
        out["risk_score"] = round(risk.score, 3)
        out["flags"] = [f.value for f in risk.flags]
        out["injection_detected"] = risk.injection_detected
    if (fit := update.get("fit_report")) is not None:
        out["fit_score"] = round(fit.score, 3)
        out["coverage"] = round(fit.coverage, 3)
    if (decision := update.get("decision")) is not None:
        out["decision"] = decision.decision.value
        out["expected_value_per_connect"] = decision.expected_value_per_connect
        out["reasons"] = decision.reasons[:3]
    if (quote := update.get("price_quote")) is not None:
        out["price_usd"] = quote.amount_usd
        out["inside_fair_band"] = quote.inside_fair_band
    if (draft := update.get("draft")) is not None:
        out["word_count"] = draft.word_count
        out["revision"] = draft.revision
    if (verification := update.get("claim_verification")) is not None:
        out["unsupported_before"] = verification.unsupported_before
        out["unsupported_after"] = verification.unsupported_after
    if (critique := update.get("critique")) is not None:
        out["approved"] = critique.approved
        out["issues"] = [i.kind for i in critique.issues]
    if (compliance := update.get("compliance")) is not None:
        out["compliance_allowed"] = compliance.allowed
        out["violations"] = [v.rule_id for v in compliance.violations]
    if update.get("errors"):
        out["errors"] = update["errors"][:2]
    if update.get("degraded_services"):
        out["degraded"] = update["degraded_services"]
    return out


def _persist_thread(
    session: Session, user: User, thread_id: str, values: dict[str, Any], *, latency_s: float
) -> None:
    row = session.get(Thread, thread_id)
    if row is None:
        row = Thread(id=thread_id, user_id=user.id, kind="bid")
        session.add(row)

    decision = values.get("decision")
    fit = values.get("fit_report")
    risk = values.get("risk_report")
    quote = values.get("price_quote")

    row.status = values.get("status", row.status)
    row.job_id = values.get("job_id", row.job_id)
    row.decision = decision.decision.value if decision else row.decision
    row.fit_score = float(fit.score) if fit else row.fit_score
    row.risk_score = float(risk.score) if risk else row.risk_score
    row.price_usd = float(quote.amount_usd) if quote else row.price_usd
    row.tokens_used = int(values.get("tokens_used", 0))
    row.cost_usd = float(values.get("cost_usd", 0.0))
    row.latency_s = round(latency_s, 3)
    row.terminal_reason = str(values.get("terminal_reason", ""))[:2000]
    row.degraded = bool(values.get("degraded_services"))
    row.summary = summarise(values)
    row.updated_at = _now()
    if values.get("job"):
        row.job_snapshot = values["job"]

    draft = values.get("draft")
    if draft is not None:
        verification = values.get("claim_verification")
        proposal = session.scalar(
            select(Proposal).where(Proposal.thread_id == thread_id)
        ) or Proposal(thread_id=thread_id, user_id=user.id, job_id=row.job_id)
        proposal.cover_letter = draft.cover_letter
        proposal.screening_answers = [a.model_dump(mode="json") for a in draft.screening_answers]
        proposal.evidence_ids = list(draft.evidence_ids)
        proposal.price_usd = float(draft.price_usd)
        proposal.word_count = int(draft.word_count)
        proposal.revision = int(draft.revision)
        proposal.status = draft.status
        if verification is not None:
            proposal.claims = [c.model_dump(mode="json") for c in verification.claims]
            proposal.unsupported_before = verification.unsupported_before
            proposal.unsupported_after = verification.unsupported_after
        submission = values.get("submission") or {}
        if submission:
            proposal.marketplace_proposal_id = submission.get("proposal_id", "")
            proposal.connects_spent = int(submission.get("connects_spent", 0))
            proposal.status = "submitted"
        proposal.updated_at = _now()
        session.add(proposal)

    if values.get("trace"):
        session.add(
            RunTrace(
                thread_id=thread_id,
                user_id=user.id,
                trace=values.get("trace", [])[-120:],
                node_timings=values.get("node_timings", {}),
                retrieval=values.get("retrieval_diagnostics", {}),
            )
        )
    session.commit()


def _persist_approval(
    session: Session, user: User, thread_id: str, payload: dict[str, Any]
) -> None:
    request = payload.get("approval_request", {})
    existing = session.scalar(
        select(ApprovalRecord).where(
            ApprovalRecord.thread_id == thread_id, ApprovalRecord.pending.is_(True)
        )
    )
    if existing is not None:
        existing.payload = request
        session.commit()
        return
    draft = request.get("draft") or {}
    reply = request.get("reply") or {}
    session.add(
        ApprovalRecord(
            thread_id=thread_id,
            user_id=user.id,
            kind=request.get("kind", "proposal"),
            payload=request,
            pending=True,
            text_before=draft.get("cover_letter") or reply.get("body") or "",
        )
    )
    session.commit()


# ---------------------------------------------------------------------------
# Approving
# ---------------------------------------------------------------------------
async def resume_with_approval(
    session: Session,
    user: User,
    thread_id: str,
    response: dict[str, Any],
    *,
    kind: str = "bid",
) -> dict[str, Any]:
    """Resume the interrupted thread and record what the human decided."""
    graph = await bid_graph() if kind == "bid" else await conversation_graph()
    config = thread_config(thread_id, user_id=user.id)

    snapshot = await graph.aget_state(config)
    if not snapshot.interrupts:
        return {
            "resumed": False,
            "reason": "This thread is not waiting for approval.",
            "status": (snapshot.values or {}).get("status"),
        }

    record = session.scalar(
        select(ApprovalRecord).where(
            ApprovalRecord.thread_id == thread_id, ApprovalRecord.pending.is_(True)
        )
    )

    started = time.perf_counter()
    await graph.ainvoke(Command(resume=response), config)
    final = await graph.aget_state(config)
    values = final.values or {}

    if record is not None:
        record.action = response.get("action", "")
        record.reason = response.get("reason", "")
        record.responded_by = response.get("responded_by", user.email)
        record.responded_at = _now()
        record.pending = False
        after = response.get("edited_cover_letter") or response.get("edited_reply_body") or ""
        record.text_after = after or record.text_before
        session.commit()

        # Learn from the edit, through the promotion rules only.
        if response.get("action") == "edit" and record.text_before and after:
            job_snapshot = (session.get(Thread, thread_id) or Thread()).job_snapshot or {}
            category = (job_snapshot.get("required_skills") or ["general"])[0]
            decisions = get_memory_store().record_edit(
                user.id,
                before=record.text_before,
                after=after,
                proposal_id=thread_id,
                job_category=category,
            )
            promoted = [d.rule.text for d in decisions if d.promoted and d.rule]
        else:
            promoted = []
    else:
        promoted = []

    _persist_thread(session, user, thread_id, values, latency_s=time.perf_counter() - started)
    return {
        "resumed": True,
        "status": values.get("status"),
        "submission": values.get("submission", {}),
        "terminal_reason": values.get("terminal_reason", ""),
        "memory_promoted": promoted,
        "awaiting_approval": bool(final.interrupts),
    }


async def pending_approvals(session: Session, user: User) -> list[dict[str, Any]]:
    rows = session.scalars(
        select(ApprovalRecord)
        .where(ApprovalRecord.user_id == user.id, ApprovalRecord.pending.is_(True))
        .order_by(ApprovalRecord.created_at.desc())
    )
    return [
        {
            "approval_id": r.id,
            "thread_id": r.thread_id,
            "kind": r.kind,
            "created_at": r.created_at.isoformat(),
            "payload": r.payload,
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Conversations
# ---------------------------------------------------------------------------
async def stream_conversation_run(
    session: Session, user: User, message: dict[str, Any], thread_id: str
) -> AsyncIterator[dict[str, Any]]:
    graph = await conversation_graph()
    config = thread_config(thread_id, user_id=user.id)
    state = new_conversation_state(
        message, user_id=user.id, persona_id=user.persona_id, thread_id=thread_id
    )
    state["style_rules"] = get_memory_store().style_rules_text(user.id)

    session.merge(
        Thread(
            id=thread_id,
            user_id=user.id,
            kind="conversation",
            job_id=message.get("job_id", ""),
            status="new",
            job_snapshot={},
        )
    )
    session.add(
        MessageRecord(
            user_id=user.id,
            thread_id=thread_id,
            job_id=message.get("job_id", ""),
            marketplace_message_id=message.get("message_id", ""),
            sender="client",
            body=message.get("body", ""),
        )
    )
    session.commit()

    yield {"event": "run_started", "thread_id": thread_id}

    async for chunk in graph.astream(state, config, stream_mode="updates"):
        for node_name, update in chunk.items():
            if node_name == "__interrupt__":
                payload = update[0].value if update else {}
                _persist_approval(session, user, thread_id, payload)
                yield {
                    "event": "approval_required",
                    "thread_id": thread_id,
                    "payload": payload.get("approval_request", {}),
                }
                continue
            summary: dict[str, Any] = {}
            if (intent := (update or {}).get("intent")) is not None:
                summary = {
                    "intent": intent.intent.value,
                    "confidence": intent.confidence,
                    "flags": [f.value for f in intent.risk_flags],
                }
            if (reply := (update or {}).get("reply")) is not None:
                summary["reply_words"] = len(reply.body.split())
                summary["slots"] = len(reply.proposed_slots)
            yield {
                "event": "node_complete",
                "thread_id": thread_id,
                "node": node_name,
                "status": (update or {}).get("status"),
                "summary": summary,
            }

    snapshot = await graph.aget_state(config)
    values = snapshot.values or {}
    row = session.get(Thread, thread_id)
    if row is not None:
        row.status = values.get("status", row.status)
        row.terminal_reason = str(values.get("terminal_reason", ""))[:2000]
        row.tokens_used = int(values.get("tokens_used", 0))
        row.cost_usd = float(values.get("cost_usd", 0.0))
        row.updated_at = _now()
        session.commit()

    yield {
        "event": "run_complete",
        "thread_id": thread_id,
        "summary": summarise(values),
        "awaiting_approval": bool(snapshot.interrupts),
    }


# ---------------------------------------------------------------------------
# Outcomes -> memory
# ---------------------------------------------------------------------------
def record_outcome(
    session: Session, user: User, *, job_id: str, proposal_id: str, state: str,
    category: str = "", rate_achieved_usd: float | None = None,
) -> dict[str, Any]:
    """An observed terminal outcome. The only thing that writes outcome memory."""
    session.add(
        OutcomeRecord(
            user_id=user.id, job_id=job_id, proposal_id=proposal_id,
            category=category, state=state, rate_achieved_usd=rate_achieved_usd,
        )
    )
    proposal = session.scalar(
        select(Proposal).where(Proposal.marketplace_proposal_id == proposal_id)
    ) or session.scalar(select(Proposal).where(Proposal.job_id == job_id))
    if proposal is not None:
        proposal.status = state
        proposal.updated_at = _now()
    session.commit()

    decision = get_memory_store().record_outcome(
        user.id,
        Outcome(job_id=job_id, proposal_id=proposal_id, category=category, state=state,
                rate_achieved_usd=rate_achieved_usd),
    )
    return {
        "recorded": True,
        "promoted": decision.promoted,
        "reason": decision.reason,
        "rule": decision.rule.text if decision.rule else None,
    }


def analytics(session: Session, user: User) -> dict[str, Any]:
    """What the Analytics screen shows. Every number traceable to a row."""
    threads = list(session.scalars(select(Thread).where(Thread.user_id == user.id)))
    proposals = list(session.scalars(select(Proposal).where(Proposal.user_id == user.id)))

    submitted = [p for p in proposals if p.status not in {"drafted", "pending_approval", "rejected"}]
    replied = [p for p in submitted if p.status in {"replied", "interview", "hired"}]
    hired = [p for p in submitted if p.status == "hired"]
    connects = sum(p.connects_spent for p in proposals)

    by_category: dict[str, dict[str, int]] = {}
    for proposal in proposals:
        thread = session.get(Thread, proposal.thread_id)
        skills = ((thread.job_snapshot if thread else {}) or {}).get("required_skills") or ["general"]
        key = skills[0]
        bucket = by_category.setdefault(key, {"submitted": 0, "replied": 0, "hired": 0})
        if proposal.status not in {"drafted", "pending_approval", "rejected"}:
            bucket["submitted"] += 1
        if proposal.status in {"replied", "interview", "hired"}:
            bucket["replied"] += 1
        if proposal.status == "hired":
            bucket["hired"] += 1

    decided = [t for t in threads if t.decision]
    latencies = sorted(t.latency_s for t in threads if t.latency_s)

    def pct(values: list[float], p: float) -> float:
        if not values:
            return 0.0
        idx = max(0, min(len(values) - 1, int(round(p / 100 * (len(values) - 1)))))
        return round(values[idx], 3)

    rejection_reasons: dict[str, int] = {}
    for thread in threads:
        if thread.decision in {"skip", "watch"} and thread.terminal_reason:
            key = thread.terminal_reason.split(";")[0][:90]
            rejection_reasons[key] = rejection_reasons.get(key, 0) + 1

    return {
        "jobs_analysed": len(threads),
        "bids": sum(1 for t in decided if t.decision == "bid"),
        "skips": sum(1 for t in decided if t.decision == "skip"),
        "watches": sum(1 for t in decided if t.decision == "watch"),
        "proposals_drafted": len(proposals),
        "proposals_submitted": len(submitted),
        "reply_rate": round(len(replied) / len(submitted), 4) if submitted else 0.0,
        "hire_rate": round(len(hired) / len(submitted), 4) if submitted else 0.0,
        "connects_spent": connects,
        "connects_per_reply": round(connects / len(replied), 2) if replied else None,
        "by_category": by_category,
        "cost_usd_total": round(sum(t.cost_usd for t in threads), 6),
        "cost_usd_per_job": round(sum(t.cost_usd for t in threads) / len(threads), 6) if threads else 0.0,
        "tokens_total": sum(t.tokens_used for t in threads),
        "latency_p50_s": pct(latencies, 50),
        "latency_p95_s": pct(latencies, 95),
        "unsupported_claims_before": sum(p.unsupported_before for p in proposals),
        "unsupported_claims_after": sum(p.unsupported_after for p in proposals),
        "top_rejection_reasons": sorted(
            rejection_reasons.items(), key=lambda kv: kv[1], reverse=True
        )[:8],
    }
