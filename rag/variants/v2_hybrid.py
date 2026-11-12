"""V2 - hybrid BM25 + dense with reciprocal rank fusion.

Embeddings are good at paraphrase and bad at proper nouns. A client asking for
"Celery" or "dbt" means those exact tools; the nearest dense neighbours are
"background jobs" and "SQL modelling", which are the right *concept* and the
wrong *evidence*. BM25 carries the literal token, dense carries the paraphrase,
and RRF merges them without needing a tuned score scale - which matters,
because cosine and BM25 scores are not comparable and any weighted sum of them
is a hyperparameter waiting to drift.
"""

from __future__ import annotations

from typing import Any

from rag.index.store import Hit
from rag.variants.base import RetrievalQuery, RetrievalVariant


class V2Hybrid(RetrievalVariant):
    id = "v2_hybrid"
    group = "retrieval"
    description = "Hybrid BM25 + dense fused with reciprocal rank fusion"

    async def _retrieve(self, query: RetrievalQuery) -> tuple[list[Hit], dict[str, Any]]:
        fetch_k = int(self.params.get("fetch_k", 40))
        rrf_k = int(self.params.get("rrf_k", 60))
        text = query.as_single_string()

        dense = self._dense(text, fetch_k, query)
        lexical = self._lexical(text, fetch_k, query)
        merged = self._rrf([dense, lexical], rrf_k=rrf_k, limit=fetch_k)

        dense_ids = {h.chunk.chunk_id for h in dense[: query.k]}
        lex_ids = {h.chunk.chunk_id for h in lexical[: query.k]}
        top_ids = {h.chunk.chunk_id for h in merged[: query.k]}

        return merged, {
            "strategy": "bm25_dense_rrf",
            "dense_candidates": len(dense),
            "lexical_candidates": len(lexical),
            "channel_overlap_at_k": len(dense_ids & lex_ids),
            "lexical_only_in_topk": len(top_ids - dense_ids),
            "dense_only_in_topk": len(top_ids - lex_ids),
        }
