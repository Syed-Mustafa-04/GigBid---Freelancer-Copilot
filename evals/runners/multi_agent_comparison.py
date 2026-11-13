"""R&D question C: is the multi-agent design worth it?

Three architectures on the same dev jobs:

(a) **ReAct agent** - one agent holding every tool, looping until it decides
    it is done. The honest baseline: this is what most "agent" projects are.
(b) **Supervisor** - a router that delegates to specialist workers, in the
    shape ``langgraph-supervisor`` produces. Implemented here directly rather
    than taking the dependency, because what is being compared is the
    *topology* - central router, sequential delegation, no parallelism, no
    structural approval gate - and an extra package would not change that.
(c) **Hand-built graph** - the shipped bid graph.

Measured: bid-or-skip accuracy, unsupported claims per proposal, compliance
violations caught, tokens, latency, and whether a human approval gate is
*structurally* enforced rather than merely prompted for.

The last one is the point. (a) and (b) can be *told* to ask before sending.
(c) cannot send without an interrupt resolving first. A prompt is a request; a
graph edge is a guarantee.

Run: python -m evals.runners.multi_agent_comparison --limit 30
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver

from agent.config import REPO_ROOT, get_config
from agent.handoffs import BidDecision, Decision, FitReport, JobSpec, ProposalDraft, RiskReport
from agent.llm import RunLedger, get_llm_client
from agent.safety import wrap_untrusted
from agent.state import new_bid_state
from evals.metrics import accuracy, precision_recall_f1, summarise_latency
from rag.retriever import get_variant
from rag.variants.base import RetrievalQuery
from rag.verify import unsupported_rate, verify_draft

RESULTS = REPO_ROOT / "evals" / "results"


@dataclass
class RunOutcome:
    architecture: str
    job_id: str
    gold: str
    predicted: str = "error"
    tokens: int = 0
    cost_usd: float = 0.0
    latency_s: float = 0.0
    unsupported_before: int = 0
    unsupported_after: int = 0
    claims_total: int = 0
    compliance_violations: int = 0
    approval_enforced: bool = False
    drafted: bool = False
    error: str = ""
    notes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# (a) Single ReAct agent with every tool
# ---------------------------------------------------------------------------
REACT_SYSTEM = """You are a freelancer's assistant. Decide whether to bid on the job below, and if
so draft a proposal.

You have these capabilities (describe which you use):
- search_evidence(requirement): find portfolio evidence
- get_client_history(client_id): the client's hiring record
- get_market_rates(skill): what comparable work pays
- create_proposal_draft(...): create a draft
- submit_proposal(...): SUBMIT - spends Connects

Rules: never claim experience the evidence does not support; do not move
off-platform; ask the freelancer before submitting anything.

Return JSON with: decision (bid|watch|skip), reasoning, cover_letter (if
bidding), evidence_ids used, and would_submit (true/false)."""


async def run_react(job: dict[str, Any], label: dict[str, Any]) -> RunOutcome:
    from pydantic import BaseModel, Field

    class ReActResult(BaseModel):
        decision: str = Field(description="bid, watch or skip")
        reasoning: str = ""
        cover_letter: str = ""
        evidence_ids: list[str] = Field(default_factory=list)
        would_submit: bool = False

    outcome = RunOutcome("react_single_agent", job["job_id"], label.get("decision", "skip"))
    client = get_llm_client()
    ledger = RunLedger(max_tokens=45000, max_usd=0.05)
    started = time.perf_counter()

    # The single agent still needs evidence; it retrieves once over the whole
    # post, which is exactly the V0 behaviour the RAG study measured.
    persona = label.get("target_persona", "p_ai_engineer")
    variant = get_variant("v0_naive")
    retrieval = await variant.retrieve(
        RetrievalQuery(
            text=f"{job['title']}\n{job['description']}",
            skills=job.get("required_skills", []),
            persona_id=persona,
            k=8,
        )
    )
    evidence = retrieval.to_evidence_items()
    bundle = "\n\n".join(f"[{e.evidence_id}] {e.title}\n{e.text[:600]}" for e in evidence[:8])

    try:
        result, _ = await client.structured(
            "writer",
            [
                {"role": "system", "content": REACT_SYSTEM},
                {
                    "role": "user",
                    "content": f"{wrap_untrusted(job['title'] + chr(10) + job['description'])}\n\n"
                               f"BUDGET: {job.get('budget')}\n\nEVIDENCE:\n{bundle}",
                },
            ],
            ReActResult,
            ledger=ledger,
        )
        outcome.predicted = result.decision.strip().lower()
        if outcome.predicted not in {"bid", "watch", "skip"}:
            outcome.predicted = "skip"

        if result.cover_letter:
            outcome.drafted = True
            draft = ProposalDraft(job_id=job["job_id"], cover_letter=result.cover_letter)
            _, report = await verify_draft(draft, evidence)
            rates = unsupported_rate(report)
            outcome.unsupported_before = report.unsupported_before
            outcome.unsupported_after = report.unsupported_before  # nothing strips them here
            outcome.claims_total = int(rates["claims_total"])
            outcome.notes.append(
                "Claims measured post hoc; this architecture has no verification step, "
                "so unsupported claims survive into the draft."
            )

        # The approval gate exists only as a sentence in the prompt.
        outcome.approval_enforced = False
        if result.would_submit:
            outcome.notes.append(
                "Agent reported it would submit. Nothing structural would have stopped it."
            )
    except Exception as exc:  # noqa: BLE001
        outcome.error = f"{type(exc).__name__}: {exc}"

    snapshot = ledger.snapshot()
    outcome.tokens = snapshot["total_tokens"]
    outcome.cost_usd = snapshot["cost_usd"]
    outcome.latency_s = time.perf_counter() - started
    return outcome


# ---------------------------------------------------------------------------
# (b) Supervisor: central router, sequential delegation
# ---------------------------------------------------------------------------
async def run_supervisor(job: dict[str, Any], label: dict[str, Any]) -> RunOutcome:
    """A router calls specialists one at a time and decides when to stop.

    The structural differences from (c) that this isolates: no parallel
    analysis, no typed hand-offs between workers (the router passes prose),
    no bounded critique loop, and no interrupt.
    """
    outcome = RunOutcome("prebuilt_supervisor", job["job_id"], label.get("decision", "skip"))
    client = get_llm_client()
    ledger = RunLedger(max_tokens=45000, max_usd=0.05)
    started = time.perf_counter()
    persona = label.get("target_persona", "p_ai_engineer")

    try:
        # Worker 1: analyse (sequential, blocking)
        spec, _ = await client.structured(
            "analyst",
            [
                {"role": "system", "content": "Extract a structured JobSpec from this job post."},
                {"role": "user", "content": wrap_untrusted(f"{job['title']}\n{job['description']}")},
            ],
            JobSpec,
            ledger=ledger,
        )
        # Worker 2: risk (waits for worker 1)
        risk, _ = await client.structured(
            "risk_assessor",
            [
                {"role": "system", "content": "Assess scam risk and client quality."},
                {"role": "user", "content": wrap_untrusted(f"{job['title']}\n{job['description']}")},
            ],
            RiskReport,
            ledger=ledger,
        )
        # Worker 3: fit (waits for worker 2)
        variant = get_variant("v0_naive")
        retrieval = await variant.retrieve(
            RetrievalQuery(
                text=f"{spec.title}\n{spec.summary}",
                requirements=spec.requirements,
                skills=spec.required_skills,
                persona_id=persona,
                k=8,
            )
        )
        evidence = retrieval.to_evidence_items()
        bundle = "\n\n".join(f"[{e.evidence_id}] {e.title}\n{e.text[:500]}" for e in evidence[:8])
        fit, _ = await client.structured(
            "fit_matcher",
            [
                {"role": "system", "content": "Score fit against the retrieved evidence."},
                {"role": "user", "content": f"REQUIREMENTS:\n{spec.requirements}\n\nEVIDENCE:\n{bundle}"},
            ],
            FitReport,
            ledger=ledger,
        )
        # Router decides
        decision, _ = await client.structured(
            "scout",
            [
                {"role": "system", "content": "Decide bid, watch or skip from the reports."},
                {
                    "role": "user",
                    "content": f"fit={fit.score:.2f} risk={risk.score:.2f} "
                               f"flags={[f.value for f in risk.flags]} budget={job.get('budget')}",
                },
            ],
            BidDecision,
            ledger=ledger,
        )
        outcome.predicted = decision.decision.value

        if decision.decision == Decision.BID:
            draft, _ = await client.structured(
                "writer",
                [
                    {"role": "system", "content": "Draft a proposal using only this evidence."},
                    {"role": "user", "content": f"JOB: {job['title']}\n\nEVIDENCE:\n{bundle}"},
                ],
                ProposalDraft,
                ledger=ledger,
            )
            outcome.drafted = True
            _, report = await verify_draft(draft, evidence)
            rates = unsupported_rate(report)
            outcome.unsupported_before = report.unsupported_before
            outcome.unsupported_after = report.unsupported_before
            outcome.claims_total = int(rates["claims_total"])
            outcome.notes.append(
                "Verification measured post hoc; the supervisor has no verify step in the loop."
            )
        outcome.approval_enforced = False
    except Exception as exc:  # noqa: BLE001
        outcome.error = f"{type(exc).__name__}: {exc}"

    snapshot = ledger.snapshot()
    outcome.tokens = snapshot["total_tokens"]
    outcome.cost_usd = snapshot["cost_usd"]
    outcome.latency_s = time.perf_counter() - started
    return outcome


# ---------------------------------------------------------------------------
# (c) The shipped graph
# ---------------------------------------------------------------------------
async def run_graph(graph, job: dict[str, Any], label: dict[str, Any]) -> RunOutcome:
    outcome = RunOutcome("hand_built_graph", job["job_id"], label.get("decision", "skip"))
    thread_id = f"cmp_{job['job_id']}"
    config = {"configurable": {"thread_id": thread_id}, "recursion_limit": 60}
    state = new_bid_state(
        job, user_id="eval", persona_id=label.get("target_persona", "p_ai_engineer"),
        thread_id=thread_id,
    )
    started = time.perf_counter()
    try:
        final = await graph.ainvoke(state, config)
        snapshot = await graph.aget_state(config)

        decision = final.get("decision")
        outcome.predicted = decision.decision.value if decision else "error"
        outcome.tokens = int(final.get("tokens_used", 0))
        outcome.cost_usd = float(final.get("cost_usd", 0.0))

        verification = final.get("claim_verification")
        if verification is not None:
            outcome.unsupported_before = verification.unsupported_before
            outcome.unsupported_after = verification.unsupported_after
            outcome.claims_total = len(verification.claims)
        compliance = final.get("compliance")
        if compliance is not None:
            outcome.compliance_violations = len(compliance.violations)
        outcome.drafted = final.get("draft") is not None

        # The decisive measurement: did execution actually stop for a human?
        outcome.approval_enforced = bool(snapshot.interrupts) or final.get(
            "approval_status"
        ) in {"approved", "edited", "rejected"}
        if snapshot.interrupts:
            outcome.notes.append("Execution suspended at the approval interrupt; nothing was sent.")
    except Exception as exc:  # noqa: BLE001
        outcome.error = f"{type(exc).__name__}: {exc}"

    outcome.latency_s = time.perf_counter() - started
    return outcome


# ---------------------------------------------------------------------------
def summarise(rows: list[RunOutcome]) -> dict[str, Any]:
    ok = [r for r in rows if not r.error]
    predicted = [r.predicted for r in ok]
    gold = [r.gold for r in ok]
    binary_pred = ["bid" if p == "bid" else "no_bid" for p in predicted]
    binary_gold = ["bid" if g == "bid" else "no_bid" for g in gold]
    drafted = [r for r in ok if r.drafted]

    return {
        "n": len(rows),
        "n_completed": len(ok),
        "n_errored": len(rows) - len(ok),
        "bid_skip_accuracy": accuracy(binary_pred, binary_gold),
        "bid_f1": precision_recall_f1(binary_pred, binary_gold, "bid")["f1"],
        "three_way_accuracy": accuracy(predicted, gold),
        "proposals_drafted": len(drafted),
        "unsupported_claims_per_proposal": round(
            sum(r.unsupported_after for r in drafted) / len(drafted), 3
        ) if drafted else 0.0,
        "unsupported_before_per_proposal": round(
            sum(r.unsupported_before for r in drafted) / len(drafted), 3
        ) if drafted else 0.0,
        "compliance_violations_caught": sum(r.compliance_violations for r in ok),
        "approval_structurally_enforced": all(r.approval_enforced for r in drafted) if drafted else False,
        "approval_enforced_count": f"{sum(1 for r in drafted if r.approval_enforced)}/{len(drafted)}",
        "tokens_per_job_mean": round(sum(r.tokens for r in ok) / len(ok), 1) if ok else 0,
        "usd_per_job_mean": round(sum(r.cost_usd for r in ok) / len(ok), 8) if ok else 0.0,
        "latency": summarise_latency([r.latency_s for r in ok]),
    }


async def main() -> None:
    parser = argparse.ArgumentParser(description="R&D question C: architecture comparison")
    parser.add_argument("--limit", type=int, default=30)
    parser.add_argument("--out", type=Path, default=RESULTS / "multi_agent_comparison.json")
    args = parser.parse_args()

    jobs = json.loads((REPO_ROOT / "evals" / "datasets" / "dev_jobs.json").read_text(encoding="utf-8"))
    labels = json.loads((REPO_ROOT / "evals" / "datasets" / "dev_labels.json").read_text(encoding="utf-8"))

    from collections import defaultdict

    by_family: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for job in jobs:
        by_family[job.get("family", "?")].append(job)
    per = max(1, args.limit // max(len(by_family), 1))
    sample = [j for group in by_family.values() for j in group[:per]]

    from agent.graphs.bid_graph import build_bid_graph

    graph = build_bid_graph(InMemorySaver())

    print(f"comparing 3 architectures on {len(sample)} jobs…")
    react_rows, supervisor_rows, graph_rows = [], [], []
    for job in sample:
        label = labels[job["job_id"]]
        react_rows.append(await run_react(job, label))
        supervisor_rows.append(await run_supervisor(job, label))
        graph_rows.append(await run_graph(graph, job, label))

    results = {
        "react_single_agent": summarise(react_rows),
        "prebuilt_supervisor": summarise(supervisor_rows),
        "hand_built_graph": summarise(graph_rows),
    }

    # Where does the single agent actually win? Honest answer required.
    single_wins = [
        r.job_id for r, g in zip(react_rows, graph_rows, strict=True)
        if r.predicted == r.gold and g.predicted != g.gold
    ]
    graph_wins = [
        g.job_id for r, g in zip(react_rows, graph_rows, strict=True)
        if g.predicted == g.gold and r.predicted != r.gold
    ]

    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "provider": "offline_stub" if get_llm_client().is_offline else "live_model",
        "provider_warning": (
            "Run on the deterministic offline provider. Token and latency figures and the "
            "structural approval finding are real; the accuracy columns are not measurements "
            "of model quality."
        ) if get_llm_client().is_offline else None,
        "n_jobs": len(sample),
        "rag_variant_used_by_baselines": "v0_naive",
        "shipped_rag_variant": get_config().get("rag.shipped_variant"),
        "results": results,
        "where_single_agent_wins": {
            "count": len(single_wins),
            "job_ids": single_wins[:10],
            "note": "Jobs the single agent got right and the graph did not. If this is non-empty, "
                    "the report must say so and explain why.",
        },
        "where_graph_wins": {"count": len(graph_wins), "job_ids": graph_wins[:10]},
        "structural_findings": {
            "approval_gate": {
                "react_single_agent": "prompt only - the agent can submit",
                "prebuilt_supervisor": "prompt only - the router can submit",
                "hand_built_graph": "interrupt() - execution cannot continue past the gate",
            },
            "claim_verification": {
                "react_single_agent": "none in the loop",
                "prebuilt_supervisor": "none in the loop",
                "hand_built_graph": "verify -> strip -> critique, before the human sees it",
            },
            "parallelism": {
                "react_single_agent": "sequential tool calls",
                "prebuilt_supervisor": "sequential delegation",
                "hand_built_graph": "Send() fan-out: analyst, risk, fit concurrently",
            },
            "durability": {
                "react_single_agent": "none - a crash loses the run",
                "prebuilt_supervisor": "none",
                "hand_built_graph": "checkpointed; resumes mid-thread after a restart",
            },
        },
        "rows": {
            "react": [r.__dict__ for r in react_rows],
            "supervisor": [r.__dict__ for r in supervisor_rows],
            "graph": [r.__dict__ for r in graph_rows],
        },
    }

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")

    print(f"\n{'architecture':24s} {'acc':>6s} {'unsup/prop':>11s} {'tokens':>8s} {'p95 s':>7s} {'approval':>10s}")
    for name, r in results.items():
        print(
            f"{name:24s} {r['bid_skip_accuracy']:6.3f} {r['unsupported_claims_per_proposal']:11.2f} "
            f"{r['tokens_per_job_mean']:8.0f} {r['latency']['p95_s']:7.2f} "
            f"{str(r['approval_structurally_enforced']):>10s}"
        )
    print(f"\nsingle agent beats the graph on {len(single_wins)} job(s); graph beats it on {len(graph_wins)}")
    print(f"written: {args.out}")


if __name__ == "__main__":
    asyncio.run(main())
