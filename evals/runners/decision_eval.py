"""Decision quality: bid-or-skip F1, scam detection, price inside the fair band.

Runs the real graph on labelled dev jobs and compares its decisions against the
gold labels produced by the generator. Stops before drafting - the decision is
what is being measured, and drafting every job would cost 10x for nothing.

The scam numbers are reported as precision AND recall separately rather than as
accuracy. On a set that is 12% scams, a system that never flags anything scores
88% accurate, and that number means nothing. Recall is what protects the user;
precision is what stops the system crying wolf until they ignore it.

Run: python -m evals.runners.decision_eval --limit 150
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver

from agent.config import REPO_ROOT, get_config
from agent.llm import get_llm_client
from agent.state import new_bid_state
from evals.metrics import (
    accuracy,
    cohens_kappa,
    macro_f1,
    percentile,
    precision_recall_f1,
    summarise_latency,
    wilson_interval,
)

RESULTS = REPO_ROOT / "evals" / "results"


async def run_one(graph, job: dict[str, Any], label: dict[str, Any]) -> dict[str, Any]:
    thread_id = f"eval_{job['job_id']}"
    config = {"configurable": {"thread_id": thread_id}, "recursion_limit": 60}
    state = new_bid_state(
        job,
        user_id="eval",
        persona_id=label.get("target_persona", "p_ai_engineer"),
        thread_id=thread_id,
    )
    started = time.perf_counter()
    try:
        final = await graph.ainvoke(state, config)
    except Exception as exc:  # noqa: BLE001
        return {
            "job_id": job["job_id"],
            "error": f"{type(exc).__name__}: {exc}",
            "predicted": "error",
            "gold": label.get("decision", "skip"),
        }
    elapsed = time.perf_counter() - started

    decision = final.get("decision")
    risk = final.get("risk_report")
    fit = final.get("fit_report")
    quote = final.get("price_quote")

    predicted = decision.decision.value if decision else "error"
    flags = [f.value for f in (risk.flags if risk else [])]
    scam_flags = {
        "off_platform_contact", "unpaid_trial", "free_work_request",
        "credential_request", "budget_mismatch",
    }
    predicted_scam = bool(set(flags) & scam_flags) or (risk.score >= 0.7 if risk else False)

    price_in_band = None
    if quote and label.get("fair_price_low") is not None and label.get("fair_price_high"):
        low = float(label["fair_price_low"])
        high = float(label["fair_price_high"])
        if high > 0:
            price_in_band = low <= quote.amount_usd <= high

    return {
        "job_id": job["job_id"],
        "family": job.get("family", ""),
        "persona": label.get("target_persona", ""),
        "predicted": predicted,
        "gold": label.get("decision", "skip"),
        "predicted_scam": predicted_scam,
        "gold_scam": bool(label.get("is_scam")),
        "has_injection": bool(label.get("has_injection")),
        "injection_detected": bool(risk.injection_detected) if risk else False,
        "complied_with_injection": False,  # asserted structurally; see injection_eval
        "risk_score": round(risk.score, 4) if risk else None,
        "fit_score": round(fit.score, 4) if fit else None,
        "price_usd": quote.amount_usd if quote else None,
        "price_in_band": price_in_band,
        "status": final.get("status"),
        "tokens": final.get("tokens_used", 0),
        "cost_usd": final.get("cost_usd", 0.0),
        "latency_s": round(elapsed, 4),
        "errors": len(final.get("errors", [])),
    }


async def main() -> None:
    parser = argparse.ArgumentParser(description="Measure decision quality on the dev set")
    parser.add_argument("--limit", type=int, default=120)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--out", type=Path, default=RESULTS / "decision_quality.json")
    args = parser.parse_args()

    jobs = json.loads((REPO_ROOT / "evals" / "datasets" / "dev_jobs.json").read_text(encoding="utf-8"))
    labels = json.loads((REPO_ROOT / "evals" / "datasets" / "dev_labels.json").read_text(encoding="utf-8"))

    # Stratify so every family is represented in a truncated run.
    by_family: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for job in jobs:
        by_family[job.get("family", "?")].append(job)
    per_family = max(1, args.limit // max(len(by_family), 1))
    sample = [j for group in by_family.values() for j in group[:per_family]]

    from agent.graphs.bid_graph import build_bid_graph

    graph = build_bid_graph(InMemorySaver())
    semaphore = asyncio.Semaphore(args.concurrency)

    async def bounded(job):
        async with semaphore:
            return await run_one(graph, job, labels[job["job_id"]])

    print(f"running {len(sample)} jobs (concurrency {args.concurrency})…")
    started = time.perf_counter()
    rows = await asyncio.gather(*(bounded(j) for j in sample))
    wall = time.perf_counter() - started

    ok = [r for r in rows if r["predicted"] != "error"]
    predicted = [r["predicted"] for r in ok]
    gold = [r["gold"] for r in ok]

    # Treat watch as a non-bid for the binary question "did we spend Connects?"
    binary_pred = ["bid" if p == "bid" else "no_bid" for p in predicted]
    binary_gold = ["bid" if g == "bid" else "no_bid" for g in gold]

    scam_pred = ["scam" if r["predicted_scam"] else "clean" for r in ok]
    scam_gold = ["scam" if r["gold_scam"] else "clean" for r in ok]

    injection_rows = [r for r in ok if r["has_injection"]]
    priced = [r for r in ok if r["price_in_band"] is not None]
    latencies = [r["latency_s"] for r in ok]

    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "provider": "offline_stub" if get_llm_client().is_offline else "live_model",
        "provider_warning": (
            "Run on the deterministic offline provider. The rule-based parts of the decision "
            "(risk markers, thresholds, expected value, pricing band) are genuine; anything "
            "depending on model judgement (fit scoring, scam reasoning) is NOT a measurement."
        ) if get_llm_client().is_offline else None,
        "embedding_backend": os.getenv("EMBEDDING_BACKEND", "local"),
        "rag_variant": get_config().get("rag.shipped_variant"),
        "n_jobs": len(sample),
        "n_completed": len(ok),
        "n_errored": len(rows) - len(ok),
        "wall_clock_s": round(wall, 2),

        "bid_or_skip": {
            "accuracy": accuracy(binary_pred, binary_gold),
            "f1_bid": precision_recall_f1(binary_pred, binary_gold, "bid"),
            "macro_f1": macro_f1(binary_pred, binary_gold, ["bid", "no_bid"]),
            "kappa_vs_gold": cohens_kappa(binary_pred, binary_gold),
        },
        "three_way": {
            "accuracy": accuracy(predicted, gold),
            "macro_f1": macro_f1(predicted, gold, ["bid", "watch", "skip"]),
            "per_class": {
                cls: precision_recall_f1(predicted, gold, cls) for cls in ("bid", "watch", "skip")
            },
        },
        "scam_detection": {
            **precision_recall_f1(scam_pred, scam_gold, "scam"),
            "recall_ci95": wilson_interval(
                sum(1 for p, g in zip(scam_pred, scam_gold, strict=True) if p == g == "scam"),
                sum(1 for g in scam_gold if g == "scam"),
            ),
            "note": "Recall protects the user; precision stops alert fatigue. Accuracy is omitted "
                    "deliberately - on a 12% positive rate it is uninformative.",
        },
        "injection_handling": {
            "n": len(injection_rows),
            "detected": sum(1 for r in injection_rows if r["injection_detected"]),
            "detection_rate": round(
                sum(1 for r in injection_rows if r["injection_detected"]) / len(injection_rows), 4
            ) if injection_rows else 0.0,
            "still_bid_on_genuine_job": sum(
                1 for r in injection_rows if r["gold"] == "bid" and r["predicted"] == "bid"
            ),
            "wrongly_skipped": sum(
                1 for r in injection_rows if r["gold"] == "bid" and r["predicted"] != "bid"
            ),
            "note": "A poisoned post is usually a genuine job. The correct behaviour is to ignore "
                    "the instruction and still judge the job on its merits.",
        },
        "pricing": {
            "n_priced": len(priced),
            "inside_fair_band": sum(1 for r in priced if r["price_in_band"]),
            "inside_band_rate": round(
                sum(1 for r in priced if r["price_in_band"]) / len(priced), 4
            ) if priced else 0.0,
        },
        "cost_and_latency": {
            "latency": summarise_latency(latencies),
            "tokens_per_job_mean": round(sum(r["tokens"] for r in ok) / len(ok), 1) if ok else 0,
            "tokens_per_job_p95": percentile([r["tokens"] for r in ok], 95),
            "usd_per_job_mean": round(sum(r["cost_usd"] for r in ok) / len(ok), 8) if ok else 0.0,
            "usd_total": round(sum(r["cost_usd"] for r in ok), 6),
        },
        "by_family": {
            family: {
                "n": len([r for r in ok if r["family"] == family]),
                "accuracy": accuracy(
                    [r["predicted"] for r in ok if r["family"] == family],
                    [r["gold"] for r in ok if r["family"] == family],
                ),
            }
            for family in sorted({r["family"] for r in ok})
        },
        "rows": rows,
    }

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    b = payload["bid_or_skip"]
    s = payload["scam_detection"]
    print(f"\nbid/skip  accuracy={b['accuracy']:.3f}  F1(bid)={b['f1_bid']['f1']:.3f}  kappa={b['kappa_vs_gold']:.3f}")
    print(f"3-way     accuracy={payload['three_way']['accuracy']:.3f}  macroF1={payload['three_way']['macro_f1']:.3f}")
    print(f"scam      precision={s['precision']:.3f}  recall={s['recall']:.3f}  F1={s['f1']:.3f}")
    print(f"injection detected={payload['injection_handling']['detection_rate']:.3f}  "
          f"wrongly_skipped={payload['injection_handling']['wrongly_skipped']}")
    print(f"pricing   inside band={payload['pricing']['inside_band_rate']:.3f}")
    print(f"latency   p50={payload['cost_and_latency']['latency']['p50_s']:.2f}s "
          f"p95={payload['cost_and_latency']['latency']['p95_s']:.2f}s")
    if payload["provider_warning"]:
        print(f"\nNOTE: {payload['provider_warning']}")
    print(f"written: {args.out}")


if __name__ == "__main__":
    asyncio.run(main())
