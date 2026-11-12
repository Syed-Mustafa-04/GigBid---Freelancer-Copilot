"""V1 - requirement decomposition: one retrieval per requirement, then merge.

A job asking for five different things is five retrieval problems. Embedding
the whole post averages them into a centroid that matches nothing precisely -
the classic multi-requirement failure. This issues one query per extracted
requirement and merges with RRF, so each requirement gets its own shot at the
index and the merged list is guaranteed to span them.

Requirements come from the Job Analyst's ``JobSpec`` (already extracted
upstream, so this costs no extra LLM call). If none are present it falls back
to splitting the post on sentence boundaries.
"""

from __future__ import annotations

import re
from typing import Any

from rag.index.store import Hit
from rag.variants.base import RetrievalQuery, RetrievalVariant


class V1Decompose(RetrievalVariant):
    id = "v1_decompose"
    group = "query"
    description = "One retrieval per extracted requirement, merged with RRF"

    def _requirements(self, query: RetrievalQuery) -> list[str]:
        if query.requirements:
            return query.requirements
        sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n-\s*", query.text) if len(s.strip()) > 25]
        return sentences or [query.text]

    async def _retrieve(self, query: RetrievalQuery) -> tuple[list[Hit], dict[str, Any]]:
        max_reqs = int(self.params.get("max_requirements", 6))
        per_req = int(self.params.get("k_per_requirement", 4))
        rrf_k = int(self.params.get("rrf_k", 60))

        requirements = self._requirements(query)[:max_reqs]
        rankings: list[list[Hit]] = []
        coverage: dict[str, list[str]] = {}

        for requirement in requirements:
            # Keep the skills in context so a bare requirement still retrieves.
            text = f"{requirement}\n{' '.join(query.skills)}" if query.skills else requirement
            hits = self._dense(text, per_req, query)
            rankings.append(hits)
            coverage[requirement[:60]] = [h.chunk.evidence_id for h in hits[:3]]

        merged = self._rrf(rankings, rrf_k=rrf_k, limit=max(query.k * 3, 24))
        return merged, {
            "strategy": "decompose_then_rrf",
            "n_requirements": len(requirements),
            "k_per_requirement": per_req,
            "requirement_coverage": coverage,
            "distinct_projects_in_topk": len(
                {h.chunk.project_id for h in merged[: query.k] if h.chunk.project_id}
            ),
        }
