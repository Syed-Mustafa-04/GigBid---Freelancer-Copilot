"""V3 - cross-encoder reranking over a hybrid candidate set.

Retrieval is recall-oriented: fetch 40, hope the right thing is in there.
Reranking is precision-oriented: a cross-encoder reads the query and each
candidate *together* rather than comparing two independently-computed vectors,
which is what lets it tell "I built a churn model" from "I consumed the output
of a churn model".

That matters here more than usual, because a proposal cites 3-5 evidence items
and the Writer only sees top-k. Precision at small k is the metric that
actually reaches the client.

Cost: one cross-encoder pass over 40 candidates per job. That is the latency
this variant has to justify, and the ablation reports it.
"""

from __future__ import annotations

from typing import Any

from rag.embeddings import get_reranker
from rag.index.store import CorpusIndex, Hit
from rag.variants.base import RetrievalQuery, RetrievalVariant


class V3Rerank(RetrievalVariant):
    id = "v3_rerank"
    group = "retrieval"
    description = "Cross-encoder reranking over hybrid candidates"

    def __init__(self, index: CorpusIndex, params: dict[str, Any] | None = None) -> None:
        super().__init__(index, params)
        self._reranker = None

    @property
    def reranker(self):
        if self._reranker is None:
            self._reranker = get_reranker()
        return self._reranker

    async def _retrieve(self, query: RetrievalQuery) -> tuple[list[Hit], dict[str, Any]]:
        fetch_k = int(self.params.get("fetch_k", 40))
        rrf_k = int(self.params.get("rrf_k", 60))
        top_n = int(self.params.get("rerank_top_n", 8))
        # How many of the fused candidates actually reach the cross-encoder.
        # Measured at ~1.3s per pair on a GPU-less host, so this parameter is
        # the difference between a 53s retrieval and a 10s one. See
        # evals/results/rag_variants.json and docs/adr/0004-rag-variant.md.
        rerank_candidates = int(self.params.get("rerank_candidates", fetch_k))
        text = query.as_single_string()

        dense = self._dense(text, fetch_k, query)
        lexical = self._lexical(text, fetch_k, query)
        fused = self._rrf([dense, lexical], rrf_k=rrf_k, limit=fetch_k)
        if not fused:
            return [], {"strategy": "rerank", "candidates": 0}

        candidates = fused[:rerank_candidates]
        tail = fused[rerank_candidates:]

        before = [h.chunk.chunk_id for h in candidates[:top_n]]
        documents = [f"{h.chunk.title}\n{h.chunk.context_prefix}\n{h.chunk.text}" for h in candidates]
        scores = self.reranker.rank(text, documents)

        reranked = [
            Hit(
                chunk=hit.chunk,
                score=float(score),
                retrieved_by=f"rerank:{self.reranker.name}",
                rank=rank,
                components={**hit.components, "fusion": hit.score},
            )
            for rank, (hit, score) in enumerate(
                sorted(zip(candidates, scores, strict=True), key=lambda pair: pair[1], reverse=True)
            )
        ]
        # Demote the un-reranked tail below everything the cross-encoder saw,
        # keeping its fusion order. Dropping it instead would throw away recall
        # the hybrid stage already paid for.
        floor = min((h.score for h in reranked), default=0.0) - 1.0
        for offset, hit in enumerate(tail):
            reranked.append(
                Hit(
                    chunk=hit.chunk,
                    score=floor - offset * 0.001,
                    retrieved_by="fusion_tail",
                    rank=len(reranked),
                    components={**hit.components, "reranked": 0.0},
                )
            )

        after = [h.chunk.chunk_id for h in reranked[:top_n]]
        moved = sum(1 for cid in after if cid not in before)

        return reranked, {
            "strategy": "hybrid_then_cross_encoder",
            "reranker": self.reranker.name,
            "candidates_fused": len(fused),
            "candidates_reranked": len(candidates),
            "candidates_skipped": len(tail),
            "promoted_into_topn": moved,
            "rank_churn_at_n": round(moved / max(top_n, 1), 3),
        }
