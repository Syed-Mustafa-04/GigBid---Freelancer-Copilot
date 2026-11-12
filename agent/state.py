"""Typed graph state and reducers (C2).

Three things are kept deliberately apart, because conflating them is what makes
agent systems unexplainable:

**State** - what this job or conversation needs *right now*. The JobSpec, the
analyses, the current draft, the decision. It is what the next node reads.

**Execution trace** - every tool call, argument, result and observation, in
order. Append-only, never read by a node for its decisions. It exists for
audit, debugging and the trace viewer in the UI.

**Long-term memory** - promoted knowledge only, written through explicit rules
(see ``agent/memory/promotion_rules.py``). Nothing reaches it automatically.
Raw tool output never reaches it at all.

Reducers matter here because Job Analyst, Risk Assessor and Fit Matcher run in
parallel and write to the same state object. Without a reducer, LangGraph
raises ``InvalidUpdateError`` on concurrent writes to one key; with the wrong
reducer, one branch silently overwrites another. Each parallel branch writes
to its *own* key, and the shared accumulators (trace, errors, tokens) use
reducers that combine rather than replace.
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, Literal, TypedDict

from agent.handoffs import (
    ApprovalRequest,
    ApprovalResponse,
    BidDecision,
    ClaimVerification,
    ClientBrief,
    ComplianceVerdict,
    Critique,
    EvidenceItem,
    FitReport,
    IntentReport,
    JobSpec,
    PriceQuote,
    ProposalDraft,
    ReplyDraft,
    RiskReport,
)


# ---------------------------------------------------------------------------
# Reducers
# ---------------------------------------------------------------------------
def merge_dicts(left: dict[str, Any] | None, right: dict[str, Any] | None) -> dict[str, Any]:
    """Shallow merge; right wins. Used for budget and diagnostic bags."""
    return {**(left or {}), **(right or {})}


def take_last(_: Any, right: Any) -> Any:
    """Last write wins. Only for keys exactly one branch writes."""
    return right


def add_tokens(left: int | None, right: int | None) -> int:
    return (left or 0) + (right or 0)


def add_floats(left: float | None, right: float | None) -> float:
    return float(left or 0.0) + float(right or 0.0)


def merge_evidence(
    left: list[EvidenceItem] | None, right: list[EvidenceItem] | None
) -> list[EvidenceItem]:
    """Union by evidence_id, keeping the highest-scoring copy and rank order."""
    out: dict[str, EvidenceItem] = {}
    for item in (left or []) + (right or []):
        existing = out.get(item.evidence_id)
        if existing is None or item.score > existing.score:
            out[item.evidence_id] = item
    return sorted(out.values(), key=lambda e: e.score, reverse=True)


# ---------------------------------------------------------------------------
# Bid graph state
# ---------------------------------------------------------------------------
class BidState(TypedDict, total=False):
    """One thread per job."""

    # -- identity ----------------------------------------------------------
    job_id: Annotated[str, take_last]
    thread_id: Annotated[str, take_last]
    user_id: Annotated[str, take_last]
    persona_id: Annotated[str, take_last]

    # -- input (untrusted) -------------------------------------------------
    job: Annotated[dict[str, Any], take_last]
    job_sanitised: Annotated[dict[str, Any], take_last]
    client: Annotated[dict[str, Any], take_last]

    # -- parallel branch outputs: one key each, so no branch overwrites -----
    job_spec: Annotated[JobSpec | None, take_last]
    risk_report: Annotated[RiskReport | None, take_last]
    fit_report: Annotated[FitReport | None, take_last]
    client_brief: Annotated[ClientBrief | None, take_last]

    # -- evidence accumulates across branches ------------------------------
    evidence: Annotated[list[EvidenceItem], merge_evidence]
    retrieval_diagnostics: Annotated[dict[str, Any], merge_dicts]

    # -- sequential stages -------------------------------------------------
    decision: Annotated[BidDecision | None, take_last]
    price_quote: Annotated[PriceQuote | None, take_last]
    draft: Annotated[ProposalDraft | None, take_last]
    claim_verification: Annotated[ClaimVerification | None, take_last]
    critique: Annotated[Critique | None, take_last]
    compliance: Annotated[ComplianceVerdict | None, take_last]

    # -- human in the loop -------------------------------------------------
    approval_request: Annotated[ApprovalRequest | None, take_last]
    approval: Annotated[ApprovalResponse | None, take_last]
    approval_status: Annotated[
        Literal["not_required", "pending", "approved", "edited", "rejected"], take_last
    ]
    approval_token: Annotated[str, take_last]

    # -- submission --------------------------------------------------------
    submission: Annotated[dict[str, Any], take_last]
    idempotency_key: Annotated[str, take_last]

    # -- control -----------------------------------------------------------
    revision_count: Annotated[int, take_last]
    step_count: Annotated[int, add_tokens]
    status: Annotated[
        Literal[
            "new", "analysing", "deciding", "pricing", "drafting", "verifying",
            "critiquing", "compliance", "awaiting_approval", "submitting",
            "done", "skipped", "needs_attention", "dead_letter",
        ],
        take_last,
    ]
    terminal_reason: Annotated[str, take_last]

    # -- accounting (C4 budgets, E5 reporting) -----------------------------
    tokens_used: Annotated[int, add_tokens]
    cost_usd: Annotated[float, add_floats]
    node_timings: Annotated[dict[str, Any], merge_dicts]

    # -- observability -----------------------------------------------------
    trace: Annotated[list[dict[str, Any]], operator.add]
    errors: Annotated[list[str], operator.add]
    degraded_services: Annotated[list[str], operator.add]

    # -- memory applied to this run ----------------------------------------
    style_rules: Annotated[list[str], take_last]
    outcome_rules: Annotated[list[str], take_last]


# ---------------------------------------------------------------------------
# Conversation graph state
# ---------------------------------------------------------------------------
class ConversationState(TypedDict, total=False):
    """One thread per client conversation."""

    thread_id: Annotated[str, take_last]
    job_id: Annotated[str, take_last]
    user_id: Annotated[str, take_last]
    persona_id: Annotated[str, take_last]

    messages: Annotated[list[dict[str, Any]], operator.add]
    latest_message: Annotated[dict[str, Any], take_last]
    latest_message_sanitised: Annotated[str, take_last]

    intent: Annotated[IntentReport | None, take_last]
    evidence: Annotated[list[EvidenceItem], merge_evidence]
    reply: Annotated[ReplyDraft | None, take_last]
    compliance: Annotated[ComplianceVerdict | None, take_last]

    approval_request: Annotated[ApprovalRequest | None, take_last]
    approval: Annotated[ApprovalResponse | None, take_last]
    approval_status: Annotated[
        Literal["not_required", "pending", "approved", "edited", "rejected"], take_last
    ]
    approval_token: Annotated[str, take_last]

    sent: Annotated[dict[str, Any], take_last]
    idempotency_key: Annotated[str, take_last]

    status: Annotated[
        Literal[
            "new", "classifying", "drafting", "compliance", "awaiting_approval",
            "sending", "done", "flagged", "needs_attention",
        ],
        take_last,
    ]
    terminal_reason: Annotated[str, take_last]

    step_count: Annotated[int, add_tokens]
    tokens_used: Annotated[int, add_tokens]
    cost_usd: Annotated[float, add_floats]
    trace: Annotated[list[dict[str, Any]], operator.add]
    errors: Annotated[list[str], operator.add]
    degraded_services: Annotated[list[str], operator.add]
    style_rules: Annotated[list[str], take_last]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def new_bid_state(
    job: dict[str, Any], *, user_id: str, persona_id: str, thread_id: str
) -> BidState:
    return BidState(
        job_id=job.get("job_id", ""),
        thread_id=thread_id,
        user_id=user_id,
        persona_id=persona_id,
        job=job,
        status="new",
        approval_status="not_required",
        revision_count=0,
        step_count=0,
        tokens_used=0,
        cost_usd=0.0,
        evidence=[],
        trace=[],
        errors=[],
        degraded_services=[],
        node_timings={},
        retrieval_diagnostics={},
        style_rules=[],
        outcome_rules=[],
    )


def new_conversation_state(
    message: dict[str, Any], *, user_id: str, persona_id: str, thread_id: str
) -> ConversationState:
    return ConversationState(
        thread_id=thread_id,
        job_id=message.get("job_id", ""),
        user_id=user_id,
        persona_id=persona_id,
        messages=[message],
        latest_message=message,
        status="new",
        approval_status="not_required",
        step_count=0,
        tokens_used=0,
        cost_usd=0.0,
        evidence=[],
        trace=[],
        errors=[],
        degraded_services=[],
        style_rules=[],
    )


def summarise(state: BidState | ConversationState) -> dict[str, Any]:
    """Compact view for the UI and the run list."""
    return {
        "thread_id": state.get("thread_id", ""),
        "job_id": state.get("job_id", ""),
        "status": state.get("status", "new"),
        "approval_status": state.get("approval_status", "not_required"),
        "steps": state.get("step_count", 0),
        "tokens": state.get("tokens_used", 0),
        "cost_usd": round(float(state.get("cost_usd", 0.0)), 6),
        "errors": len(state.get("errors", [])),
        "degraded": sorted(set(state.get("degraded_services", []))),
        "terminal_reason": state.get("terminal_reason", ""),
    }
