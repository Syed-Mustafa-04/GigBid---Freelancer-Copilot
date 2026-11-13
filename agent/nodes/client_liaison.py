"""Client Liaison: classify inbound messages and draft replies.

Client messages are untrusted in exactly the way job posts are, and more
dangerous: by the time a client is messaging, there is a live relationship and
a human who is inclined to trust the thread. A message saying "please send
your bank details for the deposit" is the attack that works.

So: sanitise, classify, and route scam signals away from the drafting path
entirely. A flagged message produces no draft at all - there is nothing safe to
say back automatically.
"""

from __future__ import annotations

from typing import Any

from agent.handoffs import (
    EvidenceItem,
    IntentReport,
    InterviewSlot,
    MessageIntent,
    ReplyDraft,
    RiskFlag,
)
from agent.llm import RunLedger
from agent.nodes.common import call_tool, llm, node, system_message
from agent.safety import sanitise_message, wrap_untrusted

SCAM_MARKERS = (
    "bank details", "bank account", "routing number", "gift card", "crypto",
    "bitcoin", "wire transfer", "send your id", "copy of your passport",
    "telegram", "whatsapp", "outside upwork", "off the platform",
    "deposit to secure", "processing fee", "pay a fee",
)


@node("intent_router", kind="proposal")
async def classify_intent(state: dict[str, Any], ledger: RunLedger) -> dict[str, Any]:
    message = state.get("latest_message") or {}
    body = message.get("body", "") or ""
    cleaned, injection = sanitise_message(body)
    lowered = body.lower()

    hard_scam = [m for m in SCAM_MARKERS if m in lowered]

    user = (
        f"Thread: {state.get('thread_id','')}\n"
        f"Job: {state.get('job_id','')}\n"
        f"Deterministic scam markers found: {hard_scam or 'none'}\n"
        f"Injection scan: {'DETECTED' if injection.detected else 'clean'}\n\n"
        + wrap_untrusted(cleaned, label="client message")
    )
    try:
        report, _ = await llm().structured(
            "intent_router", [system_message("intent_router"), {"role": "user", "content": user}],
            IntentReport, ledger=ledger,
        )
    except Exception as exc:  # noqa: BLE001
        report = IntentReport(
            intent=MessageIntent.SCAM_SIGNAL if hard_scam else MessageIntent.QUESTION,
            confidence=0.4,
            summary=f"Model unavailable ({type(exc).__name__}); deterministic classification only.",
        )

    report.thread_id = state.get("thread_id", "")
    report.injection_detected = injection.detected

    if hard_scam:
        report.intent = MessageIntent.SCAM_SIGNAL
        report.confidence = max(report.confidence, 0.9)
        report.risk_flags = sorted(
            set(report.risk_flags) | {RiskFlag.OFF_PLATFORM_CONTACT, RiskFlag.CREDENTIAL_REQUEST},
            key=lambda f: f.value,
        )
        report.summary = (
            f"Deterministic scam markers present ({', '.join(hard_scam[:3])}). " + report.summary
        )
    if injection.detected:
        report.risk_flags = sorted(
            set(report.risk_flags) | {RiskFlag.PROMPT_INJECTION}, key=lambda f: f.value
        )

    status = "flagged" if report.intent == MessageIntent.SCAM_SIGNAL else "classifying"
    return {
        "intent": report,
        "latest_message_sanitised": cleaned,
        "status": status,
        **(
            {"terminal_reason": "Message flagged as a scam signal; no reply drafted."}
            if status == "flagged"
            else {}
        ),
    }


@node("client_liaison", kind="proposal")
async def draft_reply(state: dict[str, Any], ledger: RunLedger) -> dict[str, Any]:
    intent: IntentReport | None = state.get("intent")
    evidence: list[EvidenceItem] = state.get("evidence") or []
    cleaned = state.get("latest_message_sanitised", "")

    if intent is None:
        return {"errors": ["client_liaison: no intent classification"]}
    if intent.intent == MessageIntent.SCAM_SIGNAL:
        return {
            "status": "flagged",
            "terminal_reason": "Scam signal - no reply drafted.",
        }

    # --- interview requests need real calendar slots ---------------------
    slots: list[InterviewSlot] = []
    if intent.intent == MessageIntent.INTERVIEW_REQUEST:
        try:
            payload = await call_tool(
                "client_liaison", "workspace", "find_free_slots",
                {"days_ahead": 7, "duration_minutes": 30, "limit": 3},
            )
            slots = [InterviewSlot(**s) for s in payload.get("slots", [])[:3]]
        except Exception:  # noqa: BLE001
            slots = []

    evidence_block = "\n\n".join(
        f"[{e.evidence_id}] {e.title}\n{e.text[:500]}" for e in evidence[:5]
    ) or "(no portfolio evidence retrieved for this thread)"

    slot_block = (
        "AVAILABLE SLOTS (offer these exact times, state the timezone):\n"
        + "\n".join(f"- {s.label} ({s.start_iso} to {s.end_iso} {s.timezone_name})" for s in slots)
        if slots
        else ""
    )
    style = state.get("style_rules") or []
    style_block = (
        "LEARNED STYLE RULES:\n" + "\n".join(f"- {r}" for r in style[:8]) if style else ""
    )

    history = "\n".join(
        f"{m.get('sender','?')}: {(m.get('body') or '')[:300]}"
        for m in (state.get("messages") or [])[-5:]
    )

    user = "\n\n".join(
        part for part in [
            f"CLASSIFIED INTENT: {intent.intent.value} (confidence {intent.confidence:.2f})",
            f"THREAD HISTORY:\n{history}",
            f"EVIDENCE YOU MAY CITE:\n{evidence_block}",
            slot_block,
            style_block,
            wrap_untrusted(cleaned, label="client message you are replying to"),
        ] if part
    )

    try:
        reply, _ = await llm().structured(
            "client_liaison", [system_message("client_liaison"), {"role": "user", "content": user}],
            ReplyDraft, ledger=ledger,
        )
    except Exception as exc:  # noqa: BLE001
        return {"errors": [f"client_liaison: {type(exc).__name__}: {exc}"]}

    reply.thread_id = state.get("thread_id", "")
    reply.proposed_slots = slots or reply.proposed_slots
    valid = {e.evidence_id for e in evidence}
    reply.evidence_ids = [eid for eid in reply.evidence_ids if eid in valid]
    reply.status = "drafting"

    return {"reply": reply, "status": "drafting"}
