"""The conversation graph (C1): one durable thread per client conversation.

    START -> classify_intent -> {scam -> flag -> END}
                              -> retrieve_context -> draft_reply
                                        -> compliance -> approval [interrupt]
                                                   -> {edit -> re-compliance}
                                                   -> send -> END

Shares the approval discipline with the bid graph: the same interrupt, the
same approval-token binding, the same re-check after an edit. Messages are
cheaper than proposals - they cost no Connects - but they reach the client
just as directly, and a message cannot be recalled.

The scam branch is a dead end on purpose. When the classifier flags a message
as a scam signal, there is no safe automatic reply, so the graph produces none
and surfaces it to the user instead.
"""

from __future__ import annotations

from typing import Any, Literal

from langgraph.graph import END, START, StateGraph

from agent.handoffs import MessageIntent
from agent.nodes.approval import request_message_approval, send_reply
from agent.nodes.client_liaison import classify_intent, draft_reply
from agent.nodes.common import node
from agent.nodes.compliance import check_compliance
from agent.state import ConversationState
from rag.retriever import get_variant
from rag.variants.base import RetrievalQuery


@node("retrieve_context", kind="proposal")
async def retrieve_context(state: dict[str, Any], ledger) -> dict[str, Any]:
    """Pull portfolio evidence relevant to what the client just asked.

    A reply that answers a technical question should cite the same evidence a
    proposal would, so the claim discipline is identical on both paths.
    """
    message = state.get("latest_message_sanitised") or (state.get("latest_message") or {}).get("body", "")
    if not message.strip():
        return {"evidence": []}

    result = await get_variant().retrieve(
        RetrievalQuery(
            text=message[:1500],
            persona_id=state.get("persona_id") or "p_ai_engineer",
            k=5,
            job_id=state.get("job_id", ""),
        )
    )
    return {"evidence": result.to_evidence_items()}


def route_intent(state: ConversationState) -> Literal["draft", "flag"]:
    intent = state.get("intent")
    if intent is None:
        return "flag"
    if intent.intent == MessageIntent.SCAM_SIGNAL:
        return "flag"
    return "draft"


async def flag_for_user(state: ConversationState) -> dict[str, Any]:
    intent = state.get("intent")
    flags = ", ".join(f.value for f in (intent.risk_flags if intent else []))
    return {
        "status": "flagged",
        "terminal_reason": (
            f"Flagged for your attention ({flags or 'scam signal'}). "
            "No reply was drafted - this message asks for something GigBid will not do."
        ),
    }


def route_compliance(state: ConversationState) -> Literal["approval", "stop"]:
    verdict = state.get("compliance")
    return "stop" if verdict is not None and not verdict.allowed else "approval"


async def finalise_blocked(state: ConversationState) -> dict[str, Any]:
    verdict = state.get("compliance")
    blocking = verdict.blocking if verdict else []
    return {
        "status": "needs_attention",
        "terminal_reason": "Reply blocked: " + (", ".join(v.rule_id for v in blocking) or "policy"),
    }


def route_approval(state: ConversationState) -> Literal["send", "recheck", "stop"]:
    status = state.get("approval_status")
    if status == "edited":
        return "recheck"
    if status == "approved":
        return "send"
    return "stop"


def route_recheck(state: ConversationState) -> Literal["send", "stop"]:
    verdict = state.get("compliance")
    return "stop" if verdict is not None and not verdict.allowed else "send"


async def finalise_rejected(state: ConversationState) -> dict[str, Any]:
    return {
        "status": "done",
        "terminal_reason": state.get("terminal_reason") or "Reply not approved.",
    }


def build_conversation_graph(checkpointer: Any | None = None, *, compile_graph: bool = True):
    builder = StateGraph(ConversationState)

    builder.add_node("classify_intent", classify_intent)
    builder.add_node("flag", flag_for_user)
    builder.add_node("retrieve_context", retrieve_context)
    builder.add_node("draft_reply", draft_reply)
    builder.add_node("compliance", check_compliance)
    builder.add_node("finalise_blocked", finalise_blocked)
    builder.add_node("approval", request_message_approval)
    builder.add_node("recheck_compliance", check_compliance)
    builder.add_node("finalise_rejected", finalise_rejected)
    builder.add_node("send", send_reply)

    builder.add_edge(START, "classify_intent")
    builder.add_conditional_edges(
        "classify_intent", route_intent, {"draft": "retrieve_context", "flag": "flag"}
    )
    builder.add_edge("flag", END)
    builder.add_edge("retrieve_context", "draft_reply")
    builder.add_edge("draft_reply", "compliance")
    builder.add_conditional_edges(
        "compliance", route_compliance, {"approval": "approval", "stop": "finalise_blocked"}
    )
    builder.add_edge("finalise_blocked", END)
    builder.add_conditional_edges(
        "approval",
        route_approval,
        {"send": "send", "recheck": "recheck_compliance", "stop": "finalise_rejected"},
    )
    builder.add_conditional_edges(
        "recheck_compliance", route_recheck, {"send": "send", "stop": "finalise_blocked"}
    )
    builder.add_edge("finalise_rejected", END)
    builder.add_edge("send", END)

    if not compile_graph:
        return builder
    return builder.compile(checkpointer=checkpointer)


_GRAPH = None


async def get_conversation_graph(checkpointer: Any | None = None):
    from agent.graphs.bid_graph import build_checkpointer

    global _GRAPH
    if _GRAPH is None or checkpointer is not None:
        _GRAPH = build_conversation_graph(await build_checkpointer(checkpointer))
    return _GRAPH


def reset_conversation_graph() -> None:
    global _GRAPH
    _GRAPH = None
