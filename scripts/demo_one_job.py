"""Walk one job from the inbox to an approved, submitted proposal.

The script behind the demo video. Prints each node as it completes, stops at
the approval gate, shows exactly what would be sent, then approves and submits
and proves the Connects were spent once.

Run (GigSim must be up):  python -m scripts.demo_one_job
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import pathlib
import sys
import uuid

# Windows consoles default to cp1252, which cannot encode the typographic
# characters a language model routinely emits - a non-breaking hyphen in a
# model-written proposal crashed this script after a complete, successful run.
# Reconfiguring stdout is the fix; replacing the characters would mean showing
# the user something other than what would be sent.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import httpx
from langgraph.types import Command

from agent.config import REPO_ROOT
from agent.graphs.bid_graph import build_bid_graph, build_checkpointer
from agent.llm import provider_banner
from agent.state import new_bid_state

# Use a private checkpoint database. SQLite allows exactly one writer, so a
# demo sharing the API server's file deadlocks on `database is locked` - which
# surfaced here as nodes failing silently after the analysis completed. This is
# the concrete reason the production checkpointer is Postgres; SQLite is a
# single-process convenience, and the runbook says so.
os.environ.setdefault(
    "CHECKPOINT_SQLITE_PATH",
    str(pathlib.Path(__file__).resolve().parent.parent / "demo_checkpoints.sqlite"),
)
os.environ.pop("CHECKPOINT_DATABASE_URL", None)

BASE = os.getenv("GIGSIM_BASE_URL", "http://localhost:8081")
TOKEN = os.getenv("GIGSIM_API_TOKEN", "gigsim-dev-token")
HEADERS = {"Authorization": f"Bearer {TOKEN}"}

RULE = "=" * 72


def heading(text: str) -> None:
    print(f"\n{RULE}\n{text}\n{RULE}")


async def main() -> None:
    parser = argparse.ArgumentParser(description="Demo one job end to end")
    parser.add_argument("--job-id", default=None)
    parser.add_argument("--persona", default="p_ai_engineer")
    parser.add_argument("--max-candidates", type=int, default=10)
    args = parser.parse_args()

    heading("GigBid - one job, inbox to submitted")
    print(provider_banner())

    labels_path = REPO_ROOT / "data" / "processed" / "jobs_live_labels.json"
    labels = json.loads(labels_path.read_text(encoding="utf-8")) if labels_path.exists() else {}

    async with httpx.AsyncClient(base_url=BASE, timeout=30.0, headers=HEADERS) as sim:
        health = (await sim.get("/health")).json()
        print(f"GigSim: {health['jobs_visible']} visible jobs, {health['clients']} clients")
        jobs = (await sim.get("/jobs", params={"limit": 100})).json()["jobs"]

    if args.job_id:
        candidates = [j for j in jobs if j["job_id"] == args.job_id]
    else:
        candidates = [
            j for j in jobs
            if labels.get(j["job_id"], {}).get("decision") == "bid"
            and not labels.get(j["job_id"], {}).get("has_injection")
        ] or jobs

    graph = build_bid_graph(await build_checkpointer())

    for job in candidates[: args.max_candidates]:
        thread_id = f"demo_{uuid.uuid4().hex[:10]}"
        config = {"configurable": {"thread_id": thread_id}, "recursion_limit": 60}
        state = new_bid_state(job, user_id="demo", persona_id=args.persona, thread_id=thread_id)

        heading(f"JOB  {job['job_id']}\n{job['title']}")
        budget = job.get("budget") or {}
        print(f"budget {budget.get('min')}-{budget.get('max')} {budget.get('type')} | "
              f"{job.get('connects_cost')} Connects | {job.get('proposals_received')} competing")

        print("\n--- agents ---")
        async for chunk in graph.astream(state, config, stream_mode="updates"):
            for node, update in chunk.items():
                if node == "__interrupt__":
                    continue
                bits = []
                if (spec := (update or {}).get("job_spec")) is not None:
                    bits.append(f"{len(spec.requirements)} requirements extracted")
                if (risk := (update or {}).get("risk_report")) is not None:
                    bits.append(f"risk {risk.score:.2f}")
                    if risk.injection_detected:
                        bits.append("INJECTION DETECTED and ignored")
                if (fit := (update or {}).get("fit_report")) is not None:
                    bits.append(f"fit {fit.score:.2f}, coverage {fit.coverage:.0%}")
                if (dec := (update or {}).get("decision")) is not None:
                    bits.append(f"DECISION: {dec.decision.value} (${dec.expected_value_per_connect:.2f}/Connect)")
                if (q := (update or {}).get("price_quote")) is not None:
                    bits.append(f"bid ${q.amount_usd:,.0f} (band ${q.fair_band_low:,.0f}-${q.fair_band_high:,.0f})")
                if (d := (update or {}).get("draft")) is not None:
                    bits.append(f"draft {d.word_count} words, revision {d.revision}")
                if (v := (update or {}).get("claim_verification")) is not None:
                    bits.append(f"claims: {v.unsupported_before} unsupported -> {v.unsupported_after}")
                if (c := (update or {}).get("critique")) is not None:
                    bits.append(f"critic {'approved' if c.approved else 'sent back'}")
                if (comp := (update or {}).get("compliance")) is not None:
                    bits.append(f"compliance {'clear' if comp.allowed else 'BLOCKED'}")
                # Show failures. A node that errored previously printed as a
                # blank line, which made a broken run look like a quiet one.
                for err in (update or {}).get("errors", [])[:1]:
                    bits.append(f"ERROR: {str(err)[:200]}")
                if (update or {}).get("degraded_services"):
                    bits.append(f"degraded: {update['degraded_services']}")
                print(f"  {node:20s} {'; '.join(bits) if bits else '(no output)'}")

        snapshot = await graph.aget_state(config)
        if not snapshot.interrupts:
            print(f"\n  -> no approval needed: {snapshot.values.get('terminal_reason', '')}")
            continue

        # ---- the approval gate ----
        payload = snapshot.interrupts[0].value["approval_request"]
        draft = payload["draft"]
        heading("STOPPED FOR HUMAN APPROVAL - nothing has been sent")
        print(draft["cover_letter"])
        print(f"\nevidence cited: {', '.join(draft['evidence_ids'])}")

        supported = [c for c in payload["claims"] if c["status"] == "supported"]
        removed = [c for c in payload["claims"] if c["removed"]]
        print(f"claims: {len(supported)} supported, {len(removed)} removed by the verifier")
        for claim in removed[:3]:
            print(f"  removed: \"{claim['text'][:90]}\" - {claim['verifier_note']}")

        dry = payload["dry_run"]
        print(f"\nwould call : {dry['tool_name']} on {dry['server']}")
        print(f"cost       : {dry['connects_cost']} Connects (irreversible)")
        print(f"idempotency: {dry['idempotency_key'][:24]}…")
        print(f"budget     : {payload['budget_snapshot']}")

        # ---- approve ----
        heading("APPROVING")
        async with httpx.AsyncClient(base_url=BASE, timeout=30.0, headers=HEADERS) as sim:
            before = (await sim.get("/connects/balance")).json()["balance"]

        await graph.ainvoke(
            Command(resume={"action": "approve", "responded_by": "demo", "reason": "demo run"}),
            config,
        )
        final = await graph.aget_state(config)
        submission = (final.values or {}).get("submission", {})
        print(f"submitted  : {submission.get('proposal_id')}")
        print(f"connects   : {submission.get('connects_spent')}")

        # ---- prove exactly-once ----
        await graph.ainvoke(
            Command(resume={"action": "approve", "responded_by": "demo", "reason": "replay"}),
            config,
        )
        async with httpx.AsyncClient(base_url=BASE, timeout=30.0, headers=HEADERS) as sim:
            after = (await sim.get("/connects/balance")).json()["balance"]

        heading("EXACTLY-ONCE CHECK")
        print(f"connects before approval : {before}")
        print(f"connects after approval  : {after}")
        print(f"spent                    : {before - after}")
        print(
            "replayed the same approval a second time -> "
            + ("no further spend. Correct." if before - after == submission.get("connects_spent", 0)
               else "UNEXPECTED: a replay changed the balance.")
        )
        return

    print("\nNo job in the sample reached the approval gate.")


if __name__ == "__main__":
    asyncio.run(main())
