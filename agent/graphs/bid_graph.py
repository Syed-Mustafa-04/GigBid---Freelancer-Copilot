"""The bid graph (C1): one durable thread per job.

    START
      |
      +-- Send() --> analyst ------+
      +-- Send() --> risk_assessor +--> join --> decide --> {skip, watch, bid}
      +-- Send() --> fit_matcher --+                            |
                                                                v
                              pricing -> writer -> verify_claims -> critic
                                            ^                         |
                                            +---- revise (max 2) <----+
                                                                      |
                                                                      v
                                                  compliance -> approval [interrupt]
                                                                      |
                                                            +---------+---------+
                                                          edit                approve
                                                            |                   |
                                                      re-compliance          submit
                                                                                |
                                                                               END

Design notes
------------
**Why ``Send`` rather than three plain edges.** The three analyses are
genuinely independent - none reads another's output - so running them
concurrently cuts the critical path to the slowest of the three rather than
their sum. ``Send`` also makes the fan-out explicit in the compiled graph,
which is what the rendered diagram in the report shows.

**Why a join node.** The three branches write to different state keys, so
LangGraph can merge them without a reducer conflict, but the decision needs
all three present. The join is where "did a branch fail" is answered once.

**Why the critique loop is bounded in the graph, not in the prompt.** A model
asked to stop after two revisions will sometimes not. ``revision_count`` lives
in state and the conditional edge reads it, so the bound is structural.

**Why an edit re-enters compliance.** A human editing a draft can introduce a
policy violation - most obviously by adding their own email address. Trusting
the earlier verdict after the text changed would make the guard decorative.
"""

from __future__ import annotations

import os
from typing import Any, Literal

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from agent.config import get_config
from agent.handoffs import Decision
from agent.nodes.analyst import analyse_job
from agent.nodes.approval import request_proposal_approval, submit_proposal
from agent.nodes.compliance import check_compliance
from agent.nodes.critic import critique_draft
from agent.nodes.decision import decide
from agent.nodes.fit_matcher import match_fit
from agent.nodes.pricing import price_job
from agent.nodes.risk_assessor import assess_risk
from agent.nodes.verifier import verify_claims
from agent.nodes.writer import write_proposal
from agent.state import BidState


# ---------------------------------------------------------------------------
# Fan-out / fan-in
# ---------------------------------------------------------------------------
def fan_out(state: BidState) -> list[Send]:
    """Launch the three independent analyses concurrently."""
    payload = dict(state)
    return [
        Send("analyst", payload),
        Send("risk_assessor", payload),
        Send("fit_matcher", payload),
    ]


# Analyses the decision cannot be made without. Risk is recoverable - a
# missing risk report means "unknown risk", which the rules handle - but a
# missing JobSpec or FitReport means the decision has no inputs at all.
REQUIRED_ANALYSES = ("job_spec", "fit_report")


async def join_analyses(state: BidState) -> dict[str, Any]:
    """Barrier after the parallel branches.

    Crucially, this distinguishes "we analysed it and it is a poor fit" from
    "we could not analyse it". An earlier version let a thread whose Analyst
    and Fit Matcher had both failed fall through to the decision node, which
    read fit=0.0 and reported a confident `skip` with the reason "Fit 0.00 is
    below the watch floor". That is the worst possible failure mode for this
    product: the user sees a considered rejection of a job the system never
    actually looked at, and there is nothing in the UI to tell them otherwise.
    """
    missing = [
        name for name, value in (
            ("job_spec", state.get("job_spec")),
            ("risk_report", state.get("risk_report")),
            ("fit_report", state.get("fit_report")),
        ) if value is None
    ]
    if not missing:
        return {"status": "deciding"}

    blocking = [name for name in missing if name in REQUIRED_ANALYSES]
    if blocking:
        return {
            "status": "needs_attention",
            "terminal_reason": (
                "Could not analyse this job: "
                + ", ".join(blocking)
                + " unavailable. This is NOT a judgement about the job - it was never "
                "assessed. Retry when the model provider is reachable."
            ),
            "errors": [f"join: required analyses missing: {', '.join(blocking)}"],
        }

    # Only risk is missing: degrade loudly but keep going.
    return {
        "status": "deciding",
        "errors": [f"join: proceeding without {', '.join(missing)}"],
    }


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------
def route_decision(state: BidState) -> Literal["bid", "stop"]:
    decision = state.get("decision")
    if decision is None:
        return "stop"
    return "bid" if decision.decision == Decision.BID else "stop"


def route_join(state: BidState) -> Literal["decide", "stop"]:
    """A thread that could not be analysed must not reach the decision node."""
    return "stop" if state.get("status") == "needs_attention" else "decide"


async def finalise_unanalysed(state: BidState) -> dict[str, Any]:
    """Terminal node for a thread that could not be assessed at all."""
    return {
        "status": "needs_attention",
        "terminal_reason": state.get("terminal_reason")
        or "Could not analyse this job. It has not been judged.",
    }


async def finalise_skip(state: BidState) -> dict[str, Any]:
    """Terminal node for skip/watch, so the reason is persisted and visible."""
    decision = state.get("decision")
    verdict = decision.decision.value if decision else "skip"
    reasons = "; ".join(decision.reasons) if decision else "no decision produced"
    return {
        "status": "skipped",
        "terminal_reason": f"{verdict}: {reasons}",
    }


def route_critique(state: BidState) -> Literal["revise", "compliance", "stop"]:
    critique = state.get("critique")
    if state.get("status") == "needs_attention":
        return "stop"
    if critique is None:
        return "compliance"
    if critique.approved:
        return "compliance"
    max_revisions = int(get_config().get("budgets.max_revisions", 2))
    if int(state.get("revision_count", 0)) > max_revisions:
        return "compliance"
    return "revise"


def route_compliance(state: BidState) -> Literal["approval", "stop"]:
    verdict = state.get("compliance")
    if verdict is not None and not verdict.allowed:
        return "stop"
    return "approval"


async def finalise_blocked(state: BidState) -> dict[str, Any]:
    verdict = state.get("compliance")
    blocking = verdict.blocking if verdict else []
    return {
        "status": "needs_attention",
        "terminal_reason": state.get("terminal_reason")
        or ("Blocked: " + ", ".join(v.rule_id for v in blocking) if blocking else "Blocked."),
    }


def route_approval(state: BidState) -> Literal["submit", "recheck", "stop"]:
    status = state.get("approval_status")
    if status == "edited":
        return "recheck"
    if status == "approved":
        return "submit"
    return "stop"


async def finalise_rejected(state: BidState) -> dict[str, Any]:
    return {
        "status": state.get("status", "skipped") if state.get("status") != "submitting" else "skipped",
        "terminal_reason": state.get("terminal_reason") or "Not approved.",
    }


def route_recheck(state: BidState) -> Literal["submit", "stop"]:
    """After an edit, compliance ran again. Only a clean verdict may submit."""
    verdict = state.get("compliance")
    if verdict is not None and not verdict.allowed:
        return "stop"
    return "submit"


# ---------------------------------------------------------------------------
# Checkpointer (C6)
# ---------------------------------------------------------------------------
async def build_checkpointer(explicit: Any | None = None):
    """Postgres when configured, SQLite otherwise, memory only as a last resort.

    The checkpointer is what makes an interrupted thread survive a restart, so
    an in-memory one is never acceptable outside a test.

    This is async for two reasons that only show up at runtime. First, the
    graph is driven with ``ainvoke``/``astream``, and the *synchronous*
    ``SqliteSaver`` raises ``NotImplementedError`` on every async call - a
    failure that appears only once a thread actually tries to suspend, which
    is the worst possible time to discover it. Second, ``aiosqlite.connect()``
    returns a lazy proxy whose worker thread is not started until it is
    awaited, so a saver built from an un-awaited connection fails inside
    ``setup()``.
    """
    if explicit is not None:
        return await ensure_checkpointer_ready(explicit)

    url = os.getenv("CHECKPOINT_DATABASE_URL")
    if url:
        try:
            from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
            from psycopg import AsyncConnection

            connection = await AsyncConnection.connect(
                url, autocommit=True, prepare_threshold=0
            )
            saver = AsyncPostgresSaver(connection)
            await saver.setup()
            return saver
        except Exception as exc:  # noqa: BLE001
            print(f"[graph] Postgres checkpointer unavailable ({exc}); falling back to SQLite.")

    try:
        import aiosqlite
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

        from agent.config import REPO_ROOT

        path = os.getenv("CHECKPOINT_SQLITE_PATH", str(REPO_ROOT / "gigbid_checkpoints.sqlite"))
        connection = await aiosqlite.connect(path, check_same_thread=False)
        saver = AsyncSqliteSaver(connection)
        await saver.setup()
        return saver
    except Exception as exc:  # noqa: BLE001
        print(f"[graph] SQLite checkpointer unavailable ({exc}); using in-memory (NOT durable).")
        from langgraph.checkpoint.memory import InMemorySaver

        return InMemorySaver()


async def ensure_checkpointer_ready(saver: Any) -> Any:
    """Run one-time schema setup. Safe to call repeatedly."""
    setup = getattr(saver, "setup", None)
    if setup is None:
        return saver
    try:
        result = setup()
        if hasattr(result, "__await__"):
            await result
    except Exception as exc:  # noqa: BLE001
        print(f"[graph] checkpointer setup warning: {exc}")
    return saver


# ---------------------------------------------------------------------------
# Graph
# ---------------------------------------------------------------------------
def build_bid_graph(checkpointer: Any | None = None, *, compile_graph: bool = True):
    builder = StateGraph(BidState)

    builder.add_node("analyst", analyse_job)
    builder.add_node("risk_assessor", assess_risk)
    builder.add_node("fit_matcher", match_fit)
    builder.add_node("join", join_analyses)
    builder.add_node("decide", decide)
    builder.add_node("finalise_skip", finalise_skip)
    builder.add_node("finalise_unanalysed", finalise_unanalysed)
    builder.add_node("pricing", price_job)
    builder.add_node("writer", write_proposal)
    builder.add_node("verify_claims", verify_claims)
    builder.add_node("critic", critique_draft)
    builder.add_node("compliance", check_compliance)
    builder.add_node("finalise_blocked", finalise_blocked)
    builder.add_node("approval", request_proposal_approval)
    builder.add_node("recheck_compliance", check_compliance)
    builder.add_node("finalise_rejected", finalise_rejected)
    builder.add_node("submit", submit_proposal)

    # Fan out, then join.
    builder.add_conditional_edges(START, fan_out, ["analyst", "risk_assessor", "fit_matcher"])
    for branch in ("analyst", "risk_assessor", "fit_matcher"):
        builder.add_edge(branch, "join")

    builder.add_conditional_edges(
        "join", route_join, {"decide": "decide", "stop": "finalise_unanalysed"}
    )
    builder.add_conditional_edges(
        "decide", route_decision, {"bid": "pricing", "stop": "finalise_skip"}
    )
    builder.add_edge("finalise_skip", END)
    builder.add_edge("finalise_unanalysed", END)

    builder.add_edge("pricing", "writer")
    builder.add_edge("writer", "verify_claims")
    builder.add_edge("verify_claims", "critic")
    builder.add_conditional_edges(
        "critic",
        route_critique,
        {"revise": "writer", "compliance": "compliance", "stop": "finalise_blocked"},
    )

    builder.add_conditional_edges(
        "compliance", route_compliance, {"approval": "approval", "stop": "finalise_blocked"}
    )
    builder.add_edge("finalise_blocked", END)

    builder.add_conditional_edges(
        "approval",
        route_approval,
        {"submit": "submit", "recheck": "recheck_compliance", "stop": "finalise_rejected"},
    )
    builder.add_conditional_edges(
        "recheck_compliance", route_recheck, {"submit": "submit", "stop": "finalise_blocked"}
    )
    builder.add_edge("finalise_rejected", END)
    builder.add_edge("submit", END)

    if not compile_graph:
        return builder
    return builder.compile(checkpointer=checkpointer)


_GRAPH = None


async def get_bid_graph(checkpointer: Any | None = None):
    """Compile once per process, against a ready async checkpointer."""
    global _GRAPH
    if _GRAPH is None or checkpointer is not None:
        _GRAPH = build_bid_graph(await build_checkpointer(checkpointer))
    return _GRAPH


def reset_bid_graph() -> None:
    global _GRAPH
    _GRAPH = None
