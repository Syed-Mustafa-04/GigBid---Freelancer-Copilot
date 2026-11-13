"""Human approval and submission (R4, C5).

The interrupt is the product. ``interrupt()`` suspends the graph inside the
checkpointer, so the pending approval survives an API restart, a worker crash
and a browser refresh: the thread is a row in Postgres, not an object in
memory. Resuming with ``Command(resume=...)`` continues from exactly this node.

What the human is shown is assembled here, not in the UI, so every surface
(web, API, a future CLI) shows the same thing: the draft, the evidence behind
every claim, the risk report, the price rationale, and the ``dry_run``
preview of the exact tool call that would be made and what it would cost.

Approval mints a token. The marketplace write tools refuse without one, which
is what makes "nothing is sent without approval" a structural property rather
than a convention.
"""

from __future__ import annotations

import hashlib
import secrets
import time
from typing import Any

from langgraph.types import interrupt

from agent.handoffs import (
    ApprovalAction,
    ApprovalRequest,
    ApprovalResponse,
    DryRunPreview,
    ProposalDraft,
    ReplyDraft,
    ScreeningAnswer,
)
from agent.llm import RunLedger
from agent.mcp_client import ToolUnavailable, get_registry
from agent.nodes.common import node

_TOKENS: dict[str, dict[str, Any]] = {}


def mint_approval_token(thread_id: str, payload_hash: str, approver: str) -> str:
    """A token is bound to the exact text approved."""
    token = secrets.token_urlsafe(24)
    _TOKENS[token] = {
        "thread_id": thread_id,
        "payload_hash": payload_hash,
        "approver": approver,
        "issued_at": time.time(),
    }
    return token


def verify_approval_token(token: str, thread_id: str, payload_hash: str) -> bool:
    """Reject a token issued for different text - edits invalidate approval."""
    record = _TOKENS.get(token)
    if not record:
        return False
    if record["thread_id"] != thread_id:
        return False
    if record["payload_hash"] != payload_hash:
        return False
    return time.time() - record["issued_at"] < 86400


def payload_hash(draft: ProposalDraft | ReplyDraft) -> str:
    if isinstance(draft, ProposalDraft):
        raw = draft.cover_letter + "|" + "|".join(
            a.answer for a in draft.screening_answers
        ) + f"|{draft.price_usd}"
    else:
        raw = draft.body
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def idempotency_key_for(thread_id: str, kind: str, digest: str) -> str:
    """Stable across retries and restarts, so a replay cannot double-spend."""
    return hashlib.sha256(f"{thread_id}|{kind}|{digest}".encode()).hexdigest()[:32]


# ---------------------------------------------------------------------------
# Proposal approval
# ---------------------------------------------------------------------------
@node("approval_gate", kind="proposal")
async def request_proposal_approval(state: dict[str, Any], ledger: RunLedger) -> dict[str, Any]:
    draft: ProposalDraft | None = state.get("draft")
    if draft is None:
        return {"errors": ["approval_gate: no draft"], "status": "needs_attention"}

    compliance = state.get("compliance")
    if compliance is not None and not compliance.allowed:
        return {
            "status": "needs_attention",
            "approval_status": "not_required",
            "terminal_reason": state.get("terminal_reason")
            or "Blocked by the compliance guard before approval.",
        }

    thread_id = state.get("thread_id", "")
    digest = payload_hash(draft)
    key = idempotency_key_for(thread_id, "proposal", digest)

    # --- dry run: what would actually be sent, and what it costs ----------
    dry_run: DryRunPreview | None = None
    try:
        preview = await get_registry().call(
            "submitter", "marketplace", "create_proposal_draft",
            {
                "job_id": draft.job_id,
                "cover_letter": draft.cover_letter,
                "amount_usd": draft.price_usd,
                "screening_answers": [a.model_dump() for a in draft.screening_answers],
                "idempotency_key": key,
                "dry_run": True,
            },
        )
        job = state.get("job") or {}
        dry_run = DryRunPreview(
            tool_name="create_proposal_draft -> submit_proposal",
            server="marketplace",
            arguments={
                "job_id": draft.job_id,
                "amount_usd": draft.price_usd,
                "cover_letter_words": draft.word_count,
                "screening_answers": len(draft.screening_answers),
            },
            connects_cost=int(job.get("connects_cost", 0) or 0),
            estimated_charge_usd=0.0,
            idempotency_key=key,
            would_call=str(preview)[:600],
        )
    except ToolUnavailable as exc:
        dry_run = DryRunPreview(
            tool_name="create_proposal_draft",
            server="marketplace",
            arguments={"job_id": draft.job_id},
            would_call=f"UNAVAILABLE: {exc.user_message}",
            idempotency_key=key,
        )

    request = ApprovalRequest(
        kind="proposal",
        thread_id=thread_id,
        job_id=draft.job_id,
        draft=draft,
        evidence=state.get("evidence") or [],
        claims=(state.get("claim_verification").claims if state.get("claim_verification") else []),
        risk_report=state.get("risk_report"),
        price_quote=state.get("price_quote"),
        critique=state.get("critique"),
        compliance=compliance,
        dry_run=dry_run,
        budget_snapshot={
            "tokens_used": state.get("tokens_used", 0),
            "cost_usd": round(float(state.get("cost_usd", 0.0)), 6),
            "steps": state.get("step_count", 0),
        },
    )

    # --- suspend until a human answers -----------------------------------
    raw = interrupt({"approval_request": request.model_dump(mode="json")})

    response = (
        raw if isinstance(raw, ApprovalResponse)
        else ApprovalResponse.model_validate(raw if isinstance(raw, dict) else {"action": "reject"})
    )

    if response.action == ApprovalAction.REJECT:
        return {
            "approval": response,
            "approval_status": "rejected",
            "status": "skipped",
            "terminal_reason": f"Rejected by {response.responded_by or 'the user'}: {response.reason}",
        }

    edited = draft.model_copy(deep=True)
    status = "approved"
    if response.action == ApprovalAction.EDIT:
        status = "edited"
        if response.edited_cover_letter is not None:
            edited.cover_letter = response.edited_cover_letter
        if response.edited_screening_answers is not None:
            edited.screening_answers = [
                a if isinstance(a, ScreeningAnswer) else ScreeningAnswer.model_validate(a)
                for a in response.edited_screening_answers
            ]
        if response.edited_price_usd is not None:
            edited.price_usd = response.edited_price_usd
        edited.recount()

    edited.status = "approved"
    new_digest = payload_hash(edited)
    token = mint_approval_token(thread_id, new_digest, response.responded_by or "user")

    return {
        "draft": edited,
        "approval": response,
        "approval_status": status,
        "approval_token": token,
        "idempotency_key": idempotency_key_for(thread_id, "proposal", new_digest),
        "status": "submitting",
        # An edit changes the text, so compliance must re-check it (C5).
        "compliance": None if status == "edited" else state.get("compliance"),
    }


@node("submit", kind="proposal")
async def submit_proposal(state: dict[str, Any], ledger: RunLedger) -> dict[str, Any]:
    """Create the draft on the marketplace and submit it. Exactly once."""
    draft: ProposalDraft | None = state.get("draft")
    token = state.get("approval_token", "")
    key = state.get("idempotency_key", "")
    thread_id = state.get("thread_id", "")

    if draft is None:
        return {"errors": ["submit: no draft"], "status": "needs_attention"}
    if not token or not verify_approval_token(token, thread_id, payload_hash(draft)):
        return {
            "status": "needs_attention",
            "terminal_reason": "Approval token missing or does not match the current text.",
            "errors": ["submit: refused - no valid approval token for this exact draft"],
        }

    registry = get_registry()
    try:
        created = await registry.call(
            "submitter", "marketplace", "create_proposal_draft",
            {
                "job_id": draft.job_id,
                "cover_letter": draft.cover_letter,
                "amount_usd": draft.price_usd,
                "screening_answers": [a.model_dump() for a in draft.screening_answers],
                "idempotency_key": key,
                "dry_run": False,
                "approval_token": token,
            },
        )
        if created.get("error"):
            return {
                "status": "needs_attention",
                "errors": [f"submit: {created.get('error')}: {created.get('detail','')}"],
                "terminal_reason": str(created.get("detail", created.get("error"))),
            }

        proposal_id = (created.get("proposal") or {}).get("proposal_id", "")
        if not proposal_id:
            return {
                "status": "needs_attention",
                "errors": [f"submit: no proposal id returned: {str(created)[:200]}"],
            }

        submitted = await registry.call(
            "submitter", "marketplace", "submit_proposal",
            {
                "proposal_id": proposal_id,
                "idempotency_key": key,
                "dry_run": False,
                "approval_token": token,
            },
        )
        if submitted.get("error"):
            return {
                "status": "needs_attention",
                "errors": [f"submit: {submitted.get('error')}: {submitted.get('detail','')}"],
            }

        draft.status = "submitted"
        return {
            "draft": draft,
            "submission": {
                "proposal_id": proposal_id,
                "connects_spent": submitted.get("connects_spent", 0),
                "idempotent_replay": bool(submitted.get("idempotent_replay")),
                "submitted_at": (submitted.get("proposal") or {}).get("submitted_at"),
                "trace_id": submitted.get("trace_id", ""),
            },
            "status": "done",
            "terminal_reason": "Proposal submitted.",
        }
    except ToolUnavailable as exc:
        # The thread stays resumable: the idempotency key is in state, so a
        # retry after the service returns cannot double-submit.
        return {
            "status": "needs_attention",
            "degraded_services": [exc.server],
            "errors": [f"submit: {exc.user_message}"],
            "terminal_reason": "Marketplace unavailable at submission; queued for retry.",
        }


# ---------------------------------------------------------------------------
# Message approval
# ---------------------------------------------------------------------------
@node("message_approval_gate", kind="proposal")
async def request_message_approval(state: dict[str, Any], ledger: RunLedger) -> dict[str, Any]:
    reply: ReplyDraft | None = state.get("reply")
    if reply is None:
        return {"errors": ["message_approval_gate: no reply"], "status": "needs_attention"}

    compliance = state.get("compliance")
    if compliance is not None and not compliance.allowed:
        return {
            "status": "needs_attention",
            "terminal_reason": "Reply blocked by the compliance guard.",
        }

    thread_id = state.get("thread_id", "")
    digest = payload_hash(reply)
    key = idempotency_key_for(thread_id, "message", digest)

    dry_run: DryRunPreview | None = None
    try:
        preview = await get_registry().call(
            "client_liaison", "marketplace", "send_message",
            {
                "thread_id": thread_id,
                "body": reply.body,
                "job_id": state.get("job_id", ""),
                "idempotency_key": key,
                "dry_run": True,
            },
        )
        dry_run = DryRunPreview(
            tool_name="send_message",
            server="marketplace",
            arguments={"thread_id": thread_id, "body_words": len(reply.body.split())},
            connects_cost=0,
            idempotency_key=key,
            would_call=str(preview)[:600],
        )
    except ToolUnavailable as exc:
        dry_run = DryRunPreview(
            tool_name="send_message", server="marketplace",
            arguments={"thread_id": thread_id},
            would_call=f"UNAVAILABLE: {exc.user_message}", idempotency_key=key,
        )

    request = ApprovalRequest(
        kind="message",
        thread_id=thread_id,
        job_id=state.get("job_id", ""),
        reply=reply,
        evidence=state.get("evidence") or [],
        compliance=compliance,
        dry_run=dry_run,
        budget_snapshot={
            "tokens_used": state.get("tokens_used", 0),
            "cost_usd": round(float(state.get("cost_usd", 0.0)), 6),
        },
    )

    raw = interrupt({"approval_request": request.model_dump(mode="json")})
    response = (
        raw if isinstance(raw, ApprovalResponse)
        else ApprovalResponse.model_validate(raw if isinstance(raw, dict) else {"action": "reject"})
    )

    if response.action == ApprovalAction.REJECT:
        return {
            "approval": response,
            "approval_status": "rejected",
            "status": "done",
            "terminal_reason": f"Reply rejected: {response.reason}",
        }

    edited = reply.model_copy(deep=True)
    status = "approved"
    if response.action == ApprovalAction.EDIT and response.edited_reply_body is not None:
        edited.body = response.edited_reply_body
        status = "edited"
    edited.status = "approved"

    digest = payload_hash(edited)
    return {
        "reply": edited,
        "approval": response,
        "approval_status": status,
        "approval_token": mint_approval_token(thread_id, digest, response.responded_by or "user"),
        "idempotency_key": idempotency_key_for(thread_id, "message", digest),
        "status": "sending",
    }


@node("send_message", kind="proposal")
async def send_reply(state: dict[str, Any], ledger: RunLedger) -> dict[str, Any]:
    reply: ReplyDraft | None = state.get("reply")
    token = state.get("approval_token", "")
    key = state.get("idempotency_key", "")
    thread_id = state.get("thread_id", "")

    if reply is None:
        return {"errors": ["send_message: no reply"], "status": "needs_attention"}
    if not token or not verify_approval_token(token, thread_id, payload_hash(reply)):
        return {
            "status": "needs_attention",
            "terminal_reason": "Approval token missing or does not match the reply text.",
        }

    try:
        result = await get_registry().call(
            "client_liaison", "marketplace", "send_message",
            {
                "thread_id": thread_id,
                "body": reply.body,
                "job_id": state.get("job_id", ""),
                "idempotency_key": key,
                "dry_run": False,
                "approval_token": token,
            },
        )
        if result.get("error"):
            return {"status": "needs_attention", "errors": [f"send_message: {result.get('error')}"]}
        reply.status = "sent"
        return {
            "reply": reply,
            "sent": result.get("message", {}),
            "status": "done",
            "terminal_reason": "Reply sent.",
        }
    except ToolUnavailable as exc:
        return {
            "status": "needs_attention",
            "degraded_services": [exc.server],
            "errors": [f"send_message: {exc.user_message}"],
        }
