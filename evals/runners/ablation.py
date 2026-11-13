"""Ablate the shipped retrieval pipeline one component at a time (R&D B).

The variant comparison answers "which design is best". Ablation answers the
more useful question: "which parts of the best design are actually doing the
work, and which are decoration I could delete?"

Each run disables exactly one component of the shipped pipeline and
re-measures. A component whose removal changes nothing is dead weight and the
report says so.

Components ablated:

``no_lexical``      drop the BM25 channel, dense only
``no_dense``        drop the dense channel, BM25 only
``no_rrf``          concatenate the channels instead of fusing by rank
``no_context``      strip the generated context prefix before embedding
``no_rerank``       skip the cross-encoder (where the shipped variant uses one)
``no_persona``      remove the persona filter, search all three portfolios

Reported against the shipped configuration, per metric, with the latency
each component costs.

Run: python -m evals.runners.ablation --limit 60
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import os
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent.config import REPO_ROOT, get_config
from evals.metrics import ndcg_at_k, recall_at_k, reciprocal_rank, summarise_latency
from evals.runners.rag_eval import _requirements_from, gold_evidence_for, load_dev_set
from rag.index.store import CorpusIndex
from rag.retriever import get_index, get_variant
from rag.variants.base import RetrievalQuery

RESULTS = REPO_ROOT / "evals" / "results"


@dataclass
class Ablation:
    name: str
    description: str
    what_it_removes: str


ABLATIONS: list[Ablation] = [
    Ablation("shipped", "The configured pipeline, unmodified", "nothing (baseline)"),
    Ablation("no_lexical", "Dense retrieval only", "the BM25 channel"),
    Ablation("no_dense", "BM25 only", "the dense channel"),
    Ablation("no_rrf", "Concatenate channels instead of rank fusion", "reciprocal rank fusion"),
    Ablation("no_context", "Embed chunks without their generated context line", "contextual prefixes"),
    Ablation("no_rerank", "Skip the cross-encoder", "reranking"),
    Ablation("no_persona", "Search all personas, not just the user's", "the tenancy filter"),
]


def build_index_without_context(base_index: CorpusIndex) -> CorpusIndex:
    """Re-embed the corpus with the context prefix stripped.

    Has to rebuild rather than mask, because the prefix is baked into the
    stored vectors at ingestion time. That cost is the point: contextual
    retrieval is an indexing decision, not a query-time switch.
    """
    from rag.embeddings import get_embedder

    stripped = []
    for chunk in base_index.vectors.chunks:
        clone = copy.copy(chunk)
        clone.context_prefix = ""
        stripped.append(clone)

    index = CorpusIndex(get_embedder())
    index.build(stripped, list(base_index.graph.project_meta.values()))
    return index


async def evaluate(
    ablation: Ablation,
    jobs: list[dict[str, Any]],
    labels: dict[str, Any],
    index: CorpusIndex,
    *,
    variant_id: str,
    k: int,
) -> dict[str, Any]:
    overrides: dict[str, Any] = {}
    active_index = index

    if ablation.name == "no_lexical":
        overrides["_disable_lexical"] = True
    elif ablation.name == "no_dense":
        overrides["_disable_dense"] = True
    elif ablation.name == "no_rrf":
        overrides["_disable_rrf"] = True
    elif ablation.name == "no_rerank":
        overrides["rerank_candidates"] = 0
    elif ablation.name == "no_context":
        active_index = build_index_without_context(index)

    variant = get_variant(variant_id, index=active_index, overrides=overrides)

    # The ablation hooks are honoured by monkeypatching the shared helpers on
    # this instance only, so the shipped code path is untouched.
    if overrides.get("_disable_lexical"):
        variant._lexical = lambda *a, **kw: []  # type: ignore[method-assign]
    if overrides.get("_disable_dense"):
        variant._dense = lambda *a, **kw: []  # type: ignore[method-assign]
    if overrides.get("_disable_rrf"):
        def concatenate(rankings, rrf_k=60, limit=40):
            seen, out = set(), []
            for ranking in rankings:
                for hit in ranking:
                    if hit.chunk.chunk_id in seen:
                        continue
                    seen.add(hit.chunk.chunk_id)
                    out.append(hit)
                    if len(out) >= limit:
                        return out
            return out
        variant._rrf = concatenate  # type: ignore[method-assign]

    recalls, ndcgs, mrrs, project_hits, latencies = [], [], [], [], []

    for job in jobs:
        label = labels[job["job_id"]]
        persona = label["target_persona"]
        gold = gold_evidence_for(active_index, label["source_project_id"], persona)
        if not gold:
            continue
        query = RetrievalQuery(
            text=f"{job['title']}\n{job['description']}",
            requirements=_requirements_from(job),
            skills=job.get("required_skills", []),
            persona_id=None if ablation.name == "no_persona" else persona,
            k=max(k, 10),
            job_id=job["job_id"],
        )
        started = time.perf_counter()
        try:
            result = await variant.retrieve(query)
        except Exception:  # noqa: BLE001 - an ablation may break a variant; record it
            continue
        latencies.append(time.perf_counter() - started)

        retrieved = result.evidence_ids
        recalls.append(recall_at_k(retrieved, gold, k))
        ndcgs.append(ndcg_at_k(retrieved, gold, 10))
        mrrs.append(reciprocal_rank(retrieved, gold))
        project_hits.append(
            1.0 if label["source_project_id"] in result.project_ids[:k] else 0.0
        )

    def mean(values: list[float]) -> float:
        return round(sum(values) / len(values), 4) if values else 0.0

    return {
        "ablation": ablation.name,
        "description": ablation.description,
        "removes": ablation.what_it_removes,
        "n": len(recalls),
        f"recall@{k}": mean(recalls),
        "ndcg@10": mean(ndcgs),
        "mrr": mean(mrrs),
        f"project_hit@{k}": mean(project_hits),
        "latency": summarise_latency(latencies),
    }


async def main() -> None:
    parser = argparse.ArgumentParser(description="Ablate the shipped RAG pipeline")
    parser.add_argument("--limit", type=int, default=60)
    parser.add_argument("--k", type=int, default=5)
    parser.add_argument("--variant", default=None, help="defaults to rag.shipped_variant")
    parser.add_argument("--out", type=Path, default=RESULTS / "ablation.json")
    args = parser.parse_args()

    cfg = get_config()
    variant_id = args.variant or cfg.get("rag.shipped_variant", "v2_hybrid")
    jobs, labels = load_dev_set(args.limit)
    index = get_index()

    print(f"ablating '{variant_id}' on {len(jobs)} jobs\n")
    rows: list[dict[str, Any]] = []
    for ablation in ABLATIONS:
        started = time.perf_counter()
        row = await evaluate(ablation, jobs, labels, index, variant_id=variant_id, k=args.k)
        rows.append(row)
        print(
            f"  {ablation.name:12s} recall@{args.k}={row[f'recall@{args.k}']:.3f} "
            f"ndcg@10={row['ndcg@10']:.3f} proj_hit={row[f'project_hit@{args.k}']:.3f} "
            f"p95={row['latency']['p95_s']*1000:6.0f}ms  ({time.perf_counter()-started:.1f}s)"
        )

    baseline = rows[0]
    for row in rows[1:]:
        row["delta_vs_shipped"] = {
            f"recall@{args.k}": round(row[f"recall@{args.k}"] - baseline[f"recall@{args.k}"], 4),
            "ndcg@10": round(row["ndcg@10"] - baseline["ndcg@10"], 4),
            f"project_hit@{args.k}": round(
                row[f"project_hit@{args.k}"] - baseline[f"project_hit@{args.k}"], 4
            ),
            "latency_p95_delta_ms": round(
                (row["latency"]["p95_s"] - baseline["latency"]["p95_s"]) * 1000, 1
            ),
        }
        # A component that changes nothing when removed is decoration.
        delta = row["delta_vs_shipped"]
        row["verdict"] = (
            "load-bearing" if abs(delta["ndcg@10"]) >= 0.02 or abs(delta[f"project_hit@{args.k}"]) >= 0.02
            else "no measurable effect on this corpus"
        )

    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "variant": variant_id,
        "n_jobs": len(jobs),
        "k": args.k,
        "embedding_backend": os.getenv("EMBEDDING_BACKEND", "local"),
        "results": rows,
        "reading_guide": (
            "Each row removes exactly one component from the shipped pipeline. A large "
            "negative delta means the component is doing real work; a delta near zero "
            "means it is decoration on this corpus and could be deleted for the latency. "
            "Deltas are against the 'shipped' row, not against each other."
        ),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    print("\ncomponents with no measurable effect:")
    dead = [r["ablation"] for r in rows[1:] if r.get("verdict", "").startswith("no measurable")]
    print("  " + (", ".join(dead) if dead else "none - every component is load-bearing"))
    print(f"written: {args.out}")


if __name__ == "__main__":
    asyncio.run(main())
