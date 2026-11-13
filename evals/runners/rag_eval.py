"""R&D question B: which retrieval design actually finds the right evidence?

Gold labels are knowable here because the corpus was generated from the
projects the jobs were spawned from: if job J came from project P, the gold
evidence set is exactly the evidence items belonging to P. No human judgement,
no proxy - the ground truth is construction.

Reported per variant, per persona, and broken down by requirement count, which
is where requirement decomposition is supposed to pay off and where the
baseline is supposed to fail.

Run:
    python -m evals.runners.rag_eval                    # all variants
    python -m evals.runners.rag_eval --variants v0_naive v3_rerank
    python -m evals.runners.rag_eval --limit 60 --k 5
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

from agent.config import REPO_ROOT, get_config
from evals.metrics import (
    hit_rate,
    ndcg_at_k,
    precision_at_k,
    recall_at_k,
    reciprocal_rank,
    summarise_latency,
)
from rag.retriever import ALL_VARIANTS, get_index, get_variant
from rag.variants.base import RetrievalQuery

RESULTS = REPO_ROOT / "evals" / "results"


def load_dev_set(limit: int | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    jobs = json.loads((REPO_ROOT / "evals" / "datasets" / "dev_jobs.json").read_text(encoding="utf-8"))
    labels = json.loads((REPO_ROOT / "evals" / "datasets" / "dev_labels.json").read_text(encoding="utf-8"))
    # Only jobs with a source project have gold evidence.
    jobs = [j for j in jobs if labels.get(j["job_id"], {}).get("source_project_id")]
    if limit:
        # Stratify by persona so a truncated run is still balanced.
        by_persona: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for job in jobs:
            by_persona[labels[job["job_id"]]["target_persona"]].append(job)
        per = max(1, limit // max(len(by_persona), 1))
        jobs = [j for group in by_persona.values() for j in group[:per]]
    return jobs, labels


def gold_evidence_for(index, project_id: str, persona_id: str) -> set[str]:
    """Every evidence item belonging to the source project, for this persona."""
    return {
        chunk.evidence_id
        for chunk in index.by_project.get(project_id, [])
        if chunk.persona_id == persona_id
    }


async def evaluate_variant(
    variant_id: str,
    jobs: list[dict[str, Any]],
    labels: dict[str, Any],
    index,
    *,
    k: int = 5,
    ndcg_k: int = 10,
) -> dict[str, Any]:
    variant = get_variant(variant_id, index=index)
    rows: list[dict[str, Any]] = []
    latencies: list[float] = []
    llm_calls = 0
    llm_tokens = 0

    for job in jobs:
        label = labels[job["job_id"]]
        persona = label["target_persona"]
        gold = gold_evidence_for(index, label["source_project_id"], persona)
        if not gold:
            continue

        query = RetrievalQuery(
            text=f"{job['title']}\n{job['description']}",
            requirements=_requirements_from(job),
            skills=job.get("required_skills", []),
            persona_id=persona,
            k=max(k, ndcg_k),
            job_id=job["job_id"],
        )
        started = time.perf_counter()
        result = await variant.retrieve(query)
        elapsed = time.perf_counter() - started
        latencies.append(elapsed)
        llm_calls += result.llm_calls
        llm_tokens += result.llm_tokens

        retrieved = result.evidence_ids
        # Project-level view. Item-level recall rewards a retriever for
        # returning three near-identical views of the gold project, because the
        # gold set IS that project's items. That flatters concentration and
        # punishes breadth, which is the opposite of what a five-requirement
        # job needs. `project_hit@k` asks the question the Writer actually
        # cares about: did the right project reach the top k at all?
        gold_project = label["source_project_id"]
        retrieved_projects = result.project_ids
        rows.append({
            "job_id": job["job_id"],
            "persona": persona,
            "family": job.get("family", ""),
            "n_requirements": label.get("n_requirements", 0),
            "gold_size": len(gold),
            f"recall@{k}": recall_at_k(retrieved, gold, k),
            f"precision@{k}": precision_at_k(retrieved, gold, k),
            f"ndcg@{ndcg_k}": ndcg_at_k(retrieved, gold, ndcg_k),
            "mrr": reciprocal_rank(retrieved, gold),
            f"hit@{k}": hit_rate(retrieved, gold, k),
            f"project_hit@{k}": 1.0 if gold_project in retrieved_projects[:k] else 0.0,
            "project_mrr": reciprocal_rank(retrieved_projects, [gold_project]),
            "distinct_projects": len(result.project_ids[:k]),
            "latency_s": elapsed,
            "insufficient": bool(result.diagnostics.get("insufficient_evidence")),
        })

    def mean(key: str, subset: list[dict[str, Any]] | None = None) -> float:
        data = subset if subset is not None else rows
        values = [r[key] for r in data if key in r]
        return round(sum(values) / len(values), 4) if values else 0.0

    by_persona = {
        persona: {
            "n": len([r for r in rows if r["persona"] == persona]),
            f"recall@{k}": mean(f"recall@{k}", [r for r in rows if r["persona"] == persona]),
            f"ndcg@{ndcg_k}": mean(f"ndcg@{ndcg_k}", [r for r in rows if r["persona"] == persona]),
            "mrr": mean("mrr", [r for r in rows if r["persona"] == persona]),
        }
        for persona in sorted({r["persona"] for r in rows})
    }

    # The decomposition hypothesis lives or dies in this breakdown.
    def bucket(row: dict[str, Any]) -> str:
        n = row.get("n_requirements", 0)
        return "1-3_requirements" if n <= 3 else "4+_requirements"

    by_complexity = {
        name: {
            "n": len([r for r in rows if bucket(r) == name]),
            f"recall@{k}": mean(f"recall@{k}", [r for r in rows if bucket(r) == name]),
            f"ndcg@{ndcg_k}": mean(f"ndcg@{ndcg_k}", [r for r in rows if bucket(r) == name]),
        }
        for name in ("1-3_requirements", "4+_requirements")
    }

    return {
        "variant": variant_id,
        "group": type(variant).group,
        "description": type(variant).description,
        "n_jobs": len(rows),
        f"recall@{k}": mean(f"recall@{k}"),
        f"precision@{k}": mean(f"precision@{k}"),
        f"ndcg@{ndcg_k}": mean(f"ndcg@{ndcg_k}"),
        "mrr": mean("mrr"),
        f"hit@{k}": mean(f"hit@{k}"),
        f"project_hit@{k}": mean(f"project_hit@{k}"),
        "project_mrr": mean("project_mrr"),
        "avg_distinct_projects_at_k": mean("distinct_projects"),
        "insufficient_evidence_rate": round(
            sum(1 for r in rows if r["insufficient"]) / len(rows), 4
        ) if rows else 0.0,
        "latency": summarise_latency(latencies),
        "llm_calls_total": llm_calls,
        "llm_tokens_total": llm_tokens,
        "by_persona": by_persona,
        "by_complexity": by_complexity,
        "rows": rows,
    }


def _requirements_from(job: dict[str, Any]) -> list[str]:
    """Extract requirement lines without calling the Analyst.

    The RAG comparison must isolate *retrieval*, so every variant is given the
    same requirement list produced the same cheap way. Running the Analyst here
    would make the numbers depend on an LLM whose output varies between runs.
    """
    lines = [
        line.strip("- ").strip()
        for line in job.get("description", "").splitlines()
        if line.strip().startswith("-")
    ]
    return lines or [job.get("title", "")]


async def main() -> None:
    parser = argparse.ArgumentParser(description="Compare RAG variants on the dev set")
    parser.add_argument("--variants", nargs="*", default=None)
    parser.add_argument("--limit", type=int, default=120)
    parser.add_argument("--k", type=int, default=5)
    parser.add_argument("--ndcg-k", type=int, default=10)
    parser.add_argument("--out", type=Path, default=RESULTS / "rag_variants.json")
    args = parser.parse_args()

    cfg = get_config()
    jobs, labels = load_dev_set(args.limit)
    index = get_index()
    variants = args.variants or ALL_VARIANTS

    print(f"corpus: {index.stats()}")
    print(f"evaluating {len(variants)} variants on {len(jobs)} dev jobs (k={args.k})\n")

    results: list[dict[str, Any]] = []
    for variant_id in variants:
        started = time.perf_counter()
        result = await evaluate_variant(
            variant_id, jobs, labels, index, k=args.k, ndcg_k=args.ndcg_k
        )
        results.append(result)
        print(
            f"  {variant_id:16s} recall@{args.k}={result[f'recall@{args.k}']:.3f} "
            f"ndcg@{args.ndcg_k}={result[f'ndcg@{args.ndcg_k}']:.3f} "
            f"mrr={result['mrr']:.3f} "
            f"proj_hit@{args.k}={result[f'project_hit@{args.k}']:.3f} "
            f"proj_mrr={result['project_mrr']:.3f} "
            f"projects@k={result['avg_distinct_projects_at_k']:.2f} "
            f"p95={result['latency']['p95_s']*1000:.0f}ms "
            f"({time.perf_counter()-started:.1f}s)"
        )

    baseline = next((r for r in results if r["variant"] == "v0_naive"), None)
    for result in results:
        if baseline:
            result["delta_vs_v0"] = {
                f"recall@{args.k}": round(
                    result[f"recall@{args.k}"] - baseline[f"recall@{args.k}"], 4
                ),
                f"ndcg@{args.ndcg_k}": round(
                    result[f"ndcg@{args.ndcg_k}"] - baseline[f"ndcg@{args.ndcg_k}"], 4
                ),
                "mrr": round(result["mrr"] - baseline["mrr"], 4),
                f"project_hit@{args.k}": round(
                    result[f"project_hit@{args.k}"] - baseline[f"project_hit@{args.k}"], 4
                ),
                "project_mrr": round(result["project_mrr"] - baseline["project_mrr"], 4),
            }

    ranked = sorted(results, key=lambda r: r[f"ndcg@{args.ndcg_k}"], reverse=True)
    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "dev_jobs": len(jobs),
        "k": args.k,
        "ndcg_k": args.ndcg_k,
        "embedding_backend": os.getenv("EMBEDDING_BACKEND", "local"),
        "embedder": index.embedder.name,
        "reranker_backend": cfg.get("models.reranker.backend"),
        "shipped_variant_in_config": cfg.get("rag.shipped_variant"),
        "best_by_ndcg": ranked[0]["variant"] if ranked else None,
        "best_by_project_hit": max(
            results, key=lambda r: r[f"project_hit@{args.k}"]
        )["variant"] if results else None,
        "metric_caveat": (
            "Gold evidence for a job is the evidence belonging to the single project the job "
            "was generated from. Item-level recall@k therefore REWARDS concentration: a "
            "retriever returning three views of the gold project scores higher than one "
            "returning the gold project plus four other relevant projects. project_hit@k and "
            "project_mrr are reported alongside because they ask the question the Proposal "
            "Writer actually depends on - did the right project reach the top k - without "
            "that bias. Both are reported; neither alone is the answer."
        ),
        "corpus": index.stats(),
        "results": [{k: v for k, v in r.items() if k != "rows"} for r in results],
        "per_job_rows": {r["variant"]: r["rows"] for r in results},
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    print(f"\nbest by nDCG@{args.ndcg_k}: {payload['best_by_ndcg']}")
    print(f"config ships: {payload['shipped_variant_in_config']}")
    if payload["best_by_ndcg"] != payload["shipped_variant_in_config"]:
        print(
            "  -> these differ. Either update rag.shipped_variant in agent/config.yaml, "
            "or justify the difference in the report (latency and cost are legitimate reasons)."
        )
    print(f"written: {args.out}")


if __name__ == "__main__":
    asyncio.run(main())
