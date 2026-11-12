"""V5 - multi-vector items: index the views separately, return the project once.

A project exists in this corpus three times over: a prose case study, a repo
README, and a code fragment. They describe the same work in vocabularies that
barely overlap. Two consequences:

* **Recall.** A job post written in business language matches the case study;
  one written in stack terms matches the README or the code. Indexing only one
  view loses the other half of the queries.
* **Precision.** Indexing all three *flat* - which is what V0-V4 do - lets a
  single project occupy three of the top five slots, starving every other
  requirement.

This variant keeps all three indexed, scores a project by the best view that
matched plus a bonus when several views agree, and then returns one hit per
project. Max-over-views with an agreement bonus, rather than a mean, because a
project that is a perfect match on code and irrelevant in prose is still the
right project.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from rag.index.store import Hit
from rag.variants.base import RetrievalQuery, RetrievalVariant


class V5MultiVector(RetrievalVariant):
    id = "v5_multivector"
    group = "indexing"
    description = "Views indexed separately, aggregated and returned one per project"

    async def _retrieve(self, query: RetrievalQuery) -> tuple[list[Hit], dict[str, Any]]:
        fetch_k = int(self.params.get("fetch_k", 40))
        rrf_k = int(self.params.get("rrf_k", 60))
        agreement_bonus = float(self.params.get("agreement_bonus", 0.12))
        text = query.as_single_string()

        dense = self._dense(text, fetch_k, query)
        lexical = self._lexical(text, fetch_k, query)
        candidates = self._rrf([dense, lexical], rrf_k=rrf_k, limit=fetch_k * 2)

        by_item: dict[str, list[Hit]] = defaultdict(list)
        for hit in candidates:
            key = hit.chunk.project_id or f"ev::{hit.chunk.evidence_id}"
            by_item[key].append(hit)

        aggregated: list[Hit] = []
        view_stats: dict[str, int] = defaultdict(int)
        multi_view_items = 0

        for hits in by_item.values():
            views = {h.chunk.view or h.chunk.type for h in hits}
            for view in views:
                view_stats[view] += 1
            if len(views) > 1:
                multi_view_items += 1
            best = max(hits, key=lambda h: h.score)
            score = best.score * (1.0 + agreement_bonus * (len(views) - 1))
            aggregated.append(
                Hit(
                    chunk=best.chunk,
                    score=score,
                    retrieved_by="multivector",
                    rank=0,
                    components={
                        **best.components,
                        "views_matched": len(views),
                        "views": ",".join(sorted(views)),
                        "chunks_in_item": len(hits),
                        "best_view": best.chunk.view or best.chunk.type,
                    },
                )
            )

        aggregated.sort(key=lambda h: h.score, reverse=True)
        for rank, hit in enumerate(aggregated):
            hit.rank = rank

        return aggregated, {
            "strategy": "multi_view_index_single_item_return",
            "items_aggregated": len(by_item),
            "multi_view_items": multi_view_items,
            "view_distribution": dict(view_stats),
            "distinct_projects_in_topk": len(
                {h.chunk.project_id for h in aggregated[: query.k] if h.chunk.project_id}
            ),
        }
