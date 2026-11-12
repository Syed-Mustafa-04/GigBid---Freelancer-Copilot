"""V0 - naive dense retrieval. The baseline everything else must beat.

Fixed chunks, single embedding of the whole job post, top-k cosine. No
decomposition, no lexical channel, no reranking, no project de-duplication.

On this corpus its specific failure is predictable and worth stating up front:
because every project exists as a case study, a README and a code fragment,
the top-k is dominated by three near-identical views of one or two projects,
so a job with five requirements gets evidence for one of them.
"""

from __future__ import annotations

from typing import Any

from rag.index.store import Hit
from rag.variants.base import RetrievalQuery, RetrievalVariant


class V0Naive(RetrievalVariant):
    id = "v0_naive"
    group = "baseline"
    description = "Naive dense: fixed chunks, top-k cosine over the whole job post"

    async def _retrieve(self, query: RetrievalQuery) -> tuple[list[Hit], dict[str, Any]]:
        k = int(self.params.get("k", query.k))
        hits = self._dense(query.as_single_string(), k, query)
        projects = {h.chunk.project_id for h in hits if h.chunk.project_id}
        return hits, {
            "strategy": "dense_topk",
            "k": k,
            "distinct_projects_in_topk": len(projects),
            "duplicate_project_ratio": round(1 - len(projects) / max(len(hits), 1), 3),
        }
