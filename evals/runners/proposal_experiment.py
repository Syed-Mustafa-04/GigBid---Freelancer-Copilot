"""Signature extension X2: proposal strategies as a real A/B test.

Two drafting strategies run against the same jobs in GigSim, and the outcome
is measured by the thing that actually matters - whether a simulated client
replied - rather than by a judge scoring the text.

Why this extension. Every other quality signal in this system is a proxy: the
Critic's score, the LLM judge's rubric, Recall@5. They are all opinions about
text. GigSim has hidden client preferences and decides reply/interview/hire
from them, so it provides an *end metric* that no component of GigBid can see
or optimise against directly. That makes it the only honest arbiter available.

The two strategies:

``evidence_led``   Opens with the client's problem, names one concrete project
                   with a measurable outcome, asks one specific question.
                   What the shipped Writer prompt does.
``capability_led`` Opens with the freelancer's credentials and breadth, lists
                   capabilities, closes with availability. The register most
                   freelancers actually write in.

Both are grounded identically - the claim verifier runs on both, so this is a
test of *framing*, not of honesty.

Significance is a two-proportion z-test, reported with the Wilson interval on
each arm. With the sample sizes available here the honest outcome is usually
"not significant", and that is reported rather than buried.

Run: python -m evals.runners.proposal_experiment --limit 40
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from agent.config import REPO_ROOT, get_config
from agent.handoffs import ProposalDraft
from agent.llm import RunLedger, get_llm_client
from agent.marketplace_port import get_marketplace
from evals.metrics import two_proportion_z_test, wilson_interval
from rag.retriever import get_variant
from rag.variants.base import RetrievalQuery
from rag.verify import verify_draft

RESULTS = REPO_ROOT / "evals" / "results"

STRATEGIES: dict[str, str] = {
    "evidence_led": (
        "Write a proposal that opens with the client's own problem in their words, "
        "connects it to ONE specific past project with its measurable outcome, "
        "describes a concrete first step, and ends with one genuine question about "
        "something the post left ambiguous. Plain language. No preamble, no "
        "self-description, no list of capabilities. Under 200 words."
    ),
    "capability_led": (
        "Write a proposal that opens by introducing the freelancer and their "
        "background, summarises the breadth of relevant capabilities and tools they "
        "work with, explains why they are a strong fit for this type of work, and "
        "closes by stating availability and inviting the client to get in touch. "
        "Professional tone. Under 200 words."
    ),
}


async def draft_with(
    strategy: str, job: dict[str, Any], persona: str, ledger: RunLedger
) -> tuple[ProposalDraft, int]:
    """Draft under one strategy, with identical evidence and identical verification."""
    client = get_llm_client()
    retrieval = await get_variant().retrieve(
        RetrievalQuery(
            text=f"{job['title']}\n{job.get('description','')}",
            skills=job.get("required_skills", []),
            persona_id=persona,
            k=8,
        )
    )
    evidence = retrieval.to_evidence_items()
    bundle = "\n\n".join(f"[{e.evidence_id}] {e.title}\n{e.text[:600]}" for e in evidence[:6])

    draft, _ = await client.structured(
        "writer",
        [
            {
                "role": "system",
                "content": (
                    STRATEGIES[strategy]
                    + "\n\nYou may only use the evidence provided. Never claim experience "
                    "it does not support."
                ),
            },
            {
                "role": "user",
                "content": f"JOB: {job['title']}\n\n{job.get('description','')[:1500]}\n\nEVIDENCE:\n{bundle}",
            },
        ],
        ProposalDraft,
        ledger=ledger,
    )
    draft.job_id = job["job_id"]

    # Both arms get the same honesty treatment, so framing is the only variable.
    cleaned, report = await verify_draft(draft, evidence)
    return cleaned, report.unsupported_before


async def main() -> None:
    parser = argparse.ArgumentParser(description="X2: A/B test two proposal strategies")
    parser.add_argument("--limit", type=int, default=40)
    parser.add_argument("--advance-hours", type=int, default=72)
    parser.add_argument("--out", type=Path, default=RESULTS / "proposal_experiment.json")
    args = parser.parse_args()

    cfg = get_config()
    base = cfg.get("marketplace.gigsim_base_url") or "http://localhost:8081"
    import os

    base = os.getenv("GIGSIM_BASE_URL", base)
    token = os.getenv("GIGSIM_API_TOKEN", "gigsim-dev-token")
    headers = {"Authorization": f"Bearer {token}"}

    # Fresh world so earlier runs cannot contaminate the arms.
    async with httpx.AsyncClient(base_url=base, timeout=30.0) as sim:
        await sim.post("/admin/reset")
        jobs_payload = (await sim.get("/jobs", params={"limit": args.limit * 2}, headers=headers)).json()

    jobs = jobs_payload.get("jobs", [])[: args.limit]
    if not jobs:
        print("GigSim returned no jobs. Is it running?")
        return

    port = get_marketplace()
    ledger = RunLedger(max_tokens=10**7, max_usd=10.0)
    arms: dict[str, list[dict[str, Any]]] = {name: [] for name in STRATEGIES}
    started = time.perf_counter()

    print(f"running {len(jobs)} jobs x {len(STRATEGIES)} strategies against GigSim…")
    try:
        for index, job in enumerate(jobs):
            # Alternate which strategy is drafted first, so any ordering effect
            # in the simulator is shared rather than given to one arm.
            order = list(STRATEGIES) if index % 2 == 0 else list(reversed(STRATEGIES))
            for strategy in order:
                try:
                    draft, unsupported = await draft_with(
                        strategy, job, "p_ai_engineer", ledger
                    )
                except Exception as exc:  # noqa: BLE001
                    arms[strategy].append({"job_id": job["job_id"], "error": str(exc)[:120]})
                    continue

                key = f"exp::{strategy}::{job['job_id']}::{uuid.uuid4().hex[:6]}"
                created = await port.create_proposal_draft(
                    {
                        "job_id": job["job_id"],
                        "cover_letter": draft.cover_letter,
                        "amount_usd": float((job.get("budget") or {}).get("max") or 500) * 0.8,
                        "screening_answers": [],
                        "approval_token": "experiment",
                    },
                    idempotency_key=key,
                )
                proposal_id = (created.get("proposal") or {}).get("proposal_id")
                if not proposal_id:
                    continue
                await port.submit_proposal(proposal_id, idempotency_key=key)
                arms[strategy].append({
                    "job_id": job["job_id"],
                    "proposal_id": proposal_id,
                    "words": draft.word_count,
                    "unsupported_before": unsupported,
                })

        # Let the simulated clients react.
        async with httpx.AsyncClient(base_url=base, timeout=60.0) as sim:
            await sim.post("/admin/advance", json={"hours": args.advance_hours})
            final = (await sim.get("/proposals", headers=headers)).json()
    finally:
        await port.aclose()

    by_id = {p["proposal_id"]: p for p in final.get("proposals", [])}

    summary: dict[str, Any] = {}
    for strategy, rows in arms.items():
        submitted = [r for r in rows if r.get("proposal_id")]
        statuses = [by_id.get(r["proposal_id"], {}).get("status", "unknown") for r in submitted]
        replied = sum(1 for s in statuses if s in {"replied", "interview", "hired"})
        hired = sum(1 for s in statuses if s == "hired")
        n = len(submitted)
        summary[strategy] = {
            "n_submitted": n,
            "replied": replied,
            "hired": hired,
            "reply_rate": round(replied / n, 4) if n else 0.0,
            "reply_rate_ci95": wilson_interval(replied, n),
            "hire_rate": round(hired / n, 4) if n else 0.0,
            "mean_words": round(sum(r.get("words", 0) for r in submitted) / n, 1) if n else 0,
            "unsupported_before_total": sum(r.get("unsupported_before", 0) for r in submitted),
            "errors": sum(1 for r in rows if r.get("error")),
            "status_breakdown": {s: statuses.count(s) for s in sorted(set(statuses))},
        }

    a, b = "evidence_led", "capability_led"
    test = two_proportion_z_test(
        summary[a]["replied"], summary[a]["n_submitted"],
        summary[b]["replied"], summary[b]["n_submitted"],
    )

    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "provider": "offline_stub" if get_llm_client().is_offline else "live_model",
        "provider_warning": (
            "Run on the deterministic offline provider: both arms produced the same "
            "placeholder text, so the reply rates measure GigSim's response to identical "
            "input and the comparison is meaningless. Re-run with an API key."
        ) if get_llm_client().is_offline else None,
        "n_jobs": len(jobs),
        "advance_hours": args.advance_hours,
        "strategies": {name: prompt for name, prompt in STRATEGIES.items()},
        "arms": summary,
        "significance": {
            **test,
            "arm_a": a,
            "arm_b": b,
            "interpretation": (
                f"{a} reply rate {test.get('rate_a', 0):.3f} vs {b} {test.get('rate_b', 0):.3f}; "
                + (
                    "difference is statistically significant at p<0.05."
                    if test.get("significant")
                    else "difference is NOT significant at this sample size. More jobs would be "
                         "needed before preferring either strategy."
                )
            ),
        },
        "design_notes": [
            "Both arms are grounded by the same claim verifier, so this tests framing, "
            "not honesty.",
            "Drafting order alternates per job so any ordering effect in the simulator "
            "is shared between arms.",
            "The world is reset before the run so earlier experiments cannot contaminate it.",
            "Reply rate comes from GigSim's hidden client preferences, which no component "
            "of GigBid can observe - the point of using it as the arbiter.",
        ],
        "tokens_used": ledger.snapshot(),
        "wall_clock_s": round(time.perf_counter() - started, 1),
        "raw": arms,
    }

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")

    print(f"\n{'strategy':18s} {'n':>4s} {'replied':>8s} {'reply rate':>12s} {'95% CI':>18s}")
    for name, row in summary.items():
        ci = row["reply_rate_ci95"]
        print(
            f"{name:18s} {row['n_submitted']:4d} {row['replied']:8d} "
            f"{row['reply_rate']:12.3f} {f'[{ci[0]:.2f}, {ci[1]:.2f}]':>18s}"
        )
    print(f"\n{payload['significance']['interpretation']}")
    if payload["provider_warning"]:
        print(f"\nNOTE: {payload['provider_warning']}")
    print(f"written: {args.out}")


if __name__ == "__main__":
    asyncio.run(main())
