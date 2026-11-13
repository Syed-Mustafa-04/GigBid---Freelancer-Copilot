"""The end metric: what GigSim's clients do with the proposals GigBid submits.

Every other number in this project is an opinion about text - the Critic's
score, the judge's rubric, Recall@5. They are all produced by components that
can be tuned until they say what we want. GigSim is the one arbiter that
cannot: its clients decide reply, interview and hire from hidden preferences
no part of GigBid can read, so running the real bid graph against it and
counting what came back is the closest thing to an outcome measurement this
system has.

What this runner does, and why each step is there:

* **Reset the world first.** Reply rate is a ratio over submitted proposals.
  Proposals left behind by an earlier run would land in the denominator and
  silently change the answer, so the run starts from a clean world.
* **Run the shipped graph, not a shortcut.** Skips and watches are counted
  rather than quietly dropped: "GigBid bid on 12 of 40 jobs and 4 replied" is
  a different claim from "4 of 12 replied", and both belong in the report.
* **Approve at the interrupt.** The graph cannot submit without a human, which
  is the point of the design. Here the human is the runner, approving every
  draft unedited - so what is measured is the system's own output, with no
  eval-time editing quietly improving it.
* **Advance the clock, then join on proposal_id.** The outcome does not exist
  at submission time; GigSim resolves it when the clock moves.

Rates are reported with Wilson intervals because the sample is small. A reply
rate of 0.25 from 12 proposals has a 95% interval of roughly [0.09, 0.53], and
quoting the point estimate alone would be the dishonest way to present it.

Run (GigSim must be up):
    python -m evals.runners.gigsim_outcomes
    python -m evals.runners.gigsim_outcomes --limit 20 --advance-hours 168
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from agent.config import REPO_ROOT, get_config
from agent.graphs.bid_graph import build_bid_graph
from agent.llm import get_llm_client
from agent.state import new_bid_state
from evals.metrics import summarise_latency, wilson_interval

RESULTS = REPO_ROOT / "evals" / "results"

# One persona for the whole run. Mixing personas would mean a low reply rate
# could be an evidence mismatch rather than a proposal-quality result, and
# nothing in the output would separate the two.
PERSONA = "p_ai_engineer"

# A proposal reaches exactly one terminal status, so "replied" has to be read
# as "the client responded at all": a hire is not a separate event from a
# reply, it is a reply that went further.
REPLIED_STATUSES = {"replied", "interview", "hired"}
INTERVIEW_STATUSES = {"interview", "hired"}


async def _get(
    sim: httpx.AsyncClient,
    path: str,
    headers: dict[str, str],
    params: dict[str, Any] | None = None,
    attempts: int = 4,
) -> dict[str, Any]:
    """GET with retries, because GigSim injects 5xx faults into reads on purpose.

    The adapter the graph uses has its own retry policy; these calls are the
    runner's own bookkeeping and bypass it, so they need the same tolerance or
    a 3% fault rate fails an entire evaluation every few runs.
    """
    detail = ""
    for attempt in range(attempts):
        response = await sim.get(path, headers=headers, params=params)
        if response.status_code < 400:
            return response.json()
        detail = f"HTTP {response.status_code}: {response.text[:160]}"
        await asyncio.sleep(0.3 * (attempt + 1))
    raise RuntimeError(f"GigSim GET {path} failed after {attempts} attempts: {detail}")


async def run_one(graph, job: dict[str, Any]) -> dict[str, Any]:
    """One job: analyse it, and if the graph decided to bid, approve and submit."""
    thread_id = f"outcome_{job['job_id']}"
    config = {"configurable": {"thread_id": thread_id}, "recursion_limit": 60}
    state = new_bid_state(job, user_id="eval", persona_id=PERSONA, thread_id=thread_id)

    row: dict[str, Any] = {
        "job_id": job["job_id"],
        "decision": "error",
        "proposal_id": "",
        "connects_cost": int(job.get("connects_cost", 0) or 0),
        "connects_spent": 0,
        "submitted": False,
        "words": 0,
        "amount_usd": 0.0,
        "tokens": 0,
        "cost_usd": 0.0,
        "terminal_reason": "",
        "error": "",
    }
    started = time.perf_counter()
    try:
        final = await graph.ainvoke(state, config)
        snapshot = await graph.aget_state(config)

        # The graph suspends at the approval gate. Resuming with an approve
        # action is what turns a draft into a real submission; a job that never
        # reached the gate was skipped or blocked and must not be submitted.
        if snapshot.interrupts:
            final = await graph.ainvoke(
                Command(
                    resume={
                        "action": "approve",
                        "responded_by": "gigsim_outcomes",
                        "reason": "Approved unedited so the measurement is of the graph's own draft.",
                    }
                ),
                config,
            )

        decision = final.get("decision")
        row["decision"] = decision.decision.value if decision else "error"
        row["tokens"] = int(final.get("tokens_used", 0) or 0)
        row["cost_usd"] = float(final.get("cost_usd", 0.0) or 0.0)
        row["terminal_reason"] = str(final.get("terminal_reason", ""))[:200]

        draft = final.get("draft")
        if draft is not None:
            row["words"] = int(getattr(draft, "word_count", 0) or 0)
            row["amount_usd"] = float(getattr(draft, "price_usd", 0.0) or 0.0)

        submission = final.get("submission") or {}
        if submission.get("proposal_id"):
            row["proposal_id"] = submission["proposal_id"]
            row["submitted"] = True
            row["connects_spent"] = int(submission.get("connects_spent", 0) or 0)
        elif row["decision"] == "bid":
            # A bid that did not submit is a finding, not a hole in the data:
            # compliance blocked it, or the marketplace was unavailable.
            row["error"] = "; ".join(final.get("errors", []))[:200] or "bid but no submission"
    except Exception as exc:  # noqa: BLE001
        row["error"] = f"{type(exc).__name__}: {exc}"[:200]

    row["latency_s"] = round(time.perf_counter() - started, 4)
    return row


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Measure GigSim reply and hire rate for the shipped bid graph"
    )
    parser.add_argument("--limit", type=int, default=40)
    parser.add_argument("--advance-hours", type=int, default=72)
    parser.add_argument("--out", type=Path, default=RESULTS / "gigsim_outcomes.json")
    args = parser.parse_args()

    cfg = get_config()
    base = os.getenv(
        "GIGSIM_BASE_URL", cfg.get("marketplace.gigsim_base_url") or "http://localhost:8081"
    )
    token = os.getenv("GIGSIM_API_TOKEN", "gigsim-dev-token")
    headers = {"Authorization": f"Bearer {token}"}

    async with httpx.AsyncClient(base_url=base, timeout=60.0) as sim:
        try:
            # Fresh world: proposals from an earlier run would otherwise sit in
            # the denominator of every rate below.
            await sim.post("/admin/reset")
            jobs_payload = await _get(sim, "/jobs", headers, {"limit": args.limit})
            balance_before = int((await _get(sim, "/connects/balance", headers))["balance"])
        except (httpx.HTTPError, RuntimeError) as exc:
            print(f"GigSim is not answering at {base}: {exc}")
            print("Start it with `make gigsim` and re-run.")
            return

    jobs = jobs_payload.get("jobs", [])[: args.limit]
    if not jobs:
        print("GigSim returned no jobs. Has the corpus been generated (`make seed`)?")
        return

    offline = get_llm_client().is_offline

    # In-memory checkpointing is right here and only here: the run is a single
    # process, nothing resumes it afterwards, and a shared SQLite file would
    # carry threads in from previous runs.
    graph = build_bid_graph(InMemorySaver())

    print(f"running the bid graph over {len(jobs)} GigSim jobs (persona {PERSONA})...")
    rows: list[dict[str, Any]] = []
    wall_started = time.perf_counter()
    for index, job in enumerate(jobs, start=1):
        row = await run_one(graph, job)
        rows.append(row)
        marker = row["proposal_id"] or ("ERROR" if row["error"] else "-")
        print(f"  [{index:3d}/{len(jobs)}] {job['job_id']:20s} {row['decision']:5s} {marker}")

    submitted = [r for r in rows if r["submitted"]]

    async with httpx.AsyncClient(base_url=base, timeout=60.0) as sim:
        # Nothing has an outcome until the clock moves: GigSim's clients react
        # on /admin/advance, not on submission.
        advanced = (await sim.post("/admin/advance", json={"hours": args.advance_hours})).json()
        proposals = await _get(sim, "/proposals", headers)
        balance_after = int((await _get(sim, "/connects/balance", headers))["balance"])

    by_id = {p["proposal_id"]: p for p in proposals.get("proposals", [])}
    for row in submitted:
        row["final_status"] = by_id.get(row["proposal_id"], {}).get("status", "unknown")

    statuses = [r["final_status"] for r in submitted]
    n = len(submitted)
    replied = sum(1 for s in statuses if s in REPLIED_STATUSES)
    interview = sum(1 for s in statuses if s in INTERVIEW_STATUSES)
    hired = sum(1 for s in statuses if s == "hired")

    analysed = len(rows)
    decisions = {
        name: sum(1 for r in rows if r["decision"] == name)
        for name in ("bid", "watch", "skip", "error")
    }
    connects_spent = balance_before - balance_after
    tokens_total = sum(r["tokens"] for r in rows)
    cost_total = sum(r["cost_usd"] for r in rows)
    mix = f"{decisions['bid']} / {decisions['watch']} / {decisions['skip']}"

    payload: dict[str, Any] = {
        "generated_at": datetime.now(UTC).isoformat(),
        "provider": "offline_stub" if offline else "live_model",
        "provider_warning": (
            "Run on the deterministic offline provider. The proposal TEXT is placeholder "
            "output derived from a hash, so every job receives effectively the same "
            "letter: the reply and hire rates below measure GigSim's response to "
            "identical input and are NOT a measurement of proposal quality. What is real "
            "here is the plumbing - the decision mix, Connects accounting, exactly-once "
            "submission and latency. Set an API key and re-run for a quality number."
        ) if offline else None,
        "gigsim_base_url": base,
        "persona": PERSONA,
        "advance_hours": args.advance_hours,
        "clock_after_advance": advanced.get("clock"),
        "proposals_resolved_by_advance": advanced.get("proposals_resolved", 0),
        "funnel": {
            "jobs_analysed": analysed,
            "bid": decisions["bid"],
            "watch": decisions["watch"],
            "skip": decisions["skip"],
            "errors": sum(1 for r in rows if r["error"]),
            "proposals_submitted": n,
            "replied": replied,
            "interview_or_better": interview,
            "hired": hired,
            "note": (
                "Skips and watches are counted, not dropped. A proposal holds one "
                "terminal status, so 'replied' means the client responded at all and "
                "includes those that went to interview or hire."
            ),
        },
        "reply_rate": round(replied / n, 4) if n else 0.0,
        "reply_rate_ci95": wilson_interval(replied, n),
        "interview_rate": round(interview / n, 4) if n else 0.0,
        "interview_rate_ci95": wilson_interval(interview, n),
        "hire_rate": round(hired / n, 4) if n else 0.0,
        "hire_rate_ci95": wilson_interval(hired, n),
        "bid_rate_over_jobs_analysed": round(decisions["bid"] / analysed, 4) if analysed else 0.0,
        "status_breakdown": {s: statuses.count(s) for s in sorted(set(statuses))},
        "connects": {
            "balance_before": balance_before,
            "balance_after": balance_after,
            "spent": connects_spent,
            "spent_per_submission": round(connects_spent / n, 2) if n else 0.0,
            # The number a freelancer actually feels: Connects are the scarce
            # resource and a reply is what they are being spent on.
            "per_reply": round(connects_spent / replied, 2) if replied else None,
            "per_reply_note": (
                "null when nothing replied - a cost per reply divided by zero replies "
                "would be an infinity dressed up as a metric."
            ),
        },
        "cost": {
            "tokens_total": tokens_total,
            "tokens_per_job": round(tokens_total / analysed, 1) if analysed else 0.0,
            "usd_total": round(cost_total, 6),
            "usd_per_job": round(cost_total / analysed, 8) if analysed else 0.0,
            "usd_per_submitted_proposal": round(cost_total / n, 8) if n else 0.0,
        },
        "latency": summarise_latency([r["latency_s"] for r in rows]),
        "caveats": [
            "GigSim's reply and hire decisions come from a deterministic model of hidden "
            "client preferences. The rates are a consistent arbiter for comparing GigBid "
            "variants against each other, not a prediction of real client behaviour.",
            "Every draft was approved unedited, which is deliberate: an eval-time human "
            "improving the text would be measuring the human.",
            "The world is reset before the run, so these counts cover this run only.",
        ],
        "rows": rows,
        "wall_clock_s": round(time.perf_counter() - wall_started, 1),
    }

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")

    # -- readable summary ---------------------------------------------------
    rule = "-" * 62
    print(f"\n{rule}")
    print(f"{'jobs analysed':28s} {analysed:>10d}")
    print(f"{'  bid / watch / skip':28s} {mix:>10s}")
    print(f"{'proposals submitted':28s} {n:>10d}")
    print(rule)
    for name, rate_key, ci_key, count in (
        ("reply rate", "reply_rate", "reply_rate_ci95", replied),
        ("interview or better", "interview_rate", "interview_rate_ci95", interview),
        ("hire rate", "hire_rate", "hire_rate_ci95", hired),
    ):
        low, high = payload[ci_key]
        print(
            f"{name:28s} {payload[rate_key]:>10.3f}   "
            f"[{low:.3f}, {high:.3f}]  ({count}/{n})"
        )
    print(rule)
    per_reply = payload["connects"]["per_reply"]
    per_reply_text = "n/a" if per_reply is None else f"{per_reply:.2f}"
    print(f"{'connects spent':28s} {connects_spent:>10d}")
    print(f"{'connects per reply':28s} {per_reply_text:>10s}")
    print(f"{'tokens per job':28s} {payload['cost']['tokens_per_job']:>10.1f}")
    print(f"{'USD per job':28s} {payload['cost']['usd_per_job']:>10.6f}")
    print(f"{'p95 latency (s)':28s} {payload['latency']['p95_s']:>10.2f}")
    print(rule)
    if payload["provider_warning"]:
        print(f"\nNOTE: {payload['provider_warning']}")
    print(f"\nwritten: {args.out}")


if __name__ == "__main__":
    asyncio.run(main())
