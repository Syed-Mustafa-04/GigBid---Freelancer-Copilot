"""Retriever factory: the variant is chosen by config, never defaulted in code.

``agent/config.yaml: rag.shipped_variant`` decides what the product uses, and
that line is justified by ``evals/results/rag_variants.json``. The evaluation
harness instantiates every variant through this same factory, so what ships is
exactly what was measured.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

from rag.index.store import CorpusIndex
from rag.variants.base import RetrievalQuery, RetrievalResult, RetrievalVariant
from rag.variants.v0_naive import V0Naive
from rag.variants.v1_decompose import V1Decompose
from rag.variants.v2_hybrid import V2Hybrid
from rag.variants.v3_rerank import V3Rerank
from rag.variants.v4_outcome import V4Outcome
from rag.variants.v5_multivector import V5MultiVector
from rag.variants.v6_contextual import V6Contextual
from rag.variants.v7_hyde import V7HyDE
from rag.variants.v8_corrective import V8Corrective
from rag.variants.v9_skillgraph import V9SkillGraph

REGISTRY: dict[str, type[RetrievalVariant]] = {
    V0Naive.id: V0Naive,
    V1Decompose.id: V1Decompose,
    V2Hybrid.id: V2Hybrid,
    V3Rerank.id: V3Rerank,
    V4Outcome.id: V4Outcome,
    V5MultiVector.id: V5MultiVector,
    V6Contextual.id: V6Contextual,
    V7HyDE.id: V7HyDE,
    V8Corrective.id: V8Corrective,
    V9SkillGraph.id: V9SkillGraph,
}

ALL_VARIANTS = list(REGISTRY)


def variant_groups() -> dict[str, list[str]]:
    groups: dict[str, list[str]] = {}
    for name, cls in REGISTRY.items():
        groups.setdefault(cls.group, []).append(name)
    return groups


@lru_cache(maxsize=1)
def get_index(persist_dir: str | None = None) -> CorpusIndex:
    """Build or load the corpus index once per process."""
    from agent.config import REPO_ROOT, get_config
    from rag.embeddings import get_embedder
    from rag.ingest.pipeline import build_index

    cfg = get_config()
    directory = Path(persist_dir) if persist_dir else REPO_ROOT / "rag" / "index" / "store"
    if (directory / "vectors.npy").exists():
        try:
            return CorpusIndex.load(directory, get_embedder(cfg))
        except Exception as exc:  # noqa: BLE001
            print(f"[rag] could not load persisted index ({exc}); rebuilding.")
    return build_index(config=cfg, persist=directory)


def get_variant(
    variant_id: str | None = None,
    *,
    index: CorpusIndex | None = None,
    overrides: dict[str, Any] | None = None,
) -> RetrievalVariant:
    from agent.config import get_config

    cfg = get_config()
    name = variant_id or cfg.get("rag.shipped_variant", "v0_naive")
    if name not in REGISTRY:
        raise ValueError(f"Unknown RAG variant '{name}'. Known: {sorted(REGISTRY)}")
    params = cfg.variant_params(name)
    if overrides:
        params.update(overrides)
    return REGISTRY[name](index or get_index(), params)


async def retrieve(
    query: RetrievalQuery, *, variant_id: str | None = None, index: CorpusIndex | None = None
) -> RetrievalResult:
    """Convenience entry point used by the graph nodes and the MCP server."""
    return await get_variant(variant_id, index=index).retrieve(query)
