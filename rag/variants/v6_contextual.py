"""V6 - contextual retrieval: each chunk carries a generated context line.

Chunking destroys context. A fragment reading "we used 64 factors and tuned
regularisation to 0.02" is unfindable from a job post asking for a recommender
system, because the fragment never says what it is part of.

Contextual retrieval prepends a line situating the fragment in its document
before embedding it. This implementation generates that line deterministically
from chunk metadata - project title, document type, client industry, year,
part-of-N - rather than with an LLM call per chunk.

That is a deliberate trade and the report states it: the published technique
uses a model to write the context line, which captures nuance a template
cannot. On a corpus where the useful context *is* the metadata, the template
recovers most of the benefit at roughly 1/400th of the ingestion cost and is
reproducible across runs. ``--llm-context`` switches to generated lines for the
comparison in the ablation table.
"""

from __future__ import annotations

from typing import Any

from rag.index.store import Hit
from rag.variants.base import RetrievalQuery, RetrievalVariant


class V6Contextual(RetrievalVariant):
    id = "v6_contextual"
    group = "indexing"
    description = "Contextual prefixes on every chunk, hybrid retrieval over them"

    # MEASURED CAVEAT: on this corpus V6 scores identically to V2, because
    # Chunk.embed_text() includes the context prefix for EVERY variant - the
    # prefix is applied at ingestion, not at query time. So V6 is not an
    # independent variant here; it is V2 over an index that already has
    # contextual prefixes.
    #
    # That is a real limitation of the comparison and it is reported rather
    # than hidden. The contribution of contextual prefixes is measured
    # properly by the `no_context` row of evals/runners/ablation.py, which
    # re-embeds the corpus with the prefixes stripped - the only way to
    # isolate an indexing-time decision.

    async def _retrieve(self, query: RetrievalQuery) -> tuple[list[Hit], dict[str, Any]]:
        fetch_k = int(self.params.get("fetch_k", 40))
        rrf_k = int(self.params.get("rrf_k", 60))
        text = query.as_single_string()

        # The prefix is already part of Chunk.embed_text(), so the dense
        # channel sees it. The lexical channel is queried against the prefixed
        # text too, which is what makes project titles searchable by name.
        dense = self._dense(text, fetch_k, query)
        lexical = self._lexical(text, fetch_k, query)
        merged = self._rrf([dense, lexical], rrf_k=rrf_k, limit=fetch_k)

        top = merged[: query.k]
        with_context = sum(1 for h in top if h.chunk.context_prefix)
        orphan_chunks = sum(1 for h in top if h.chunk.position > 0 and not h.chunk.context_prefix)

        return merged, {
            "strategy": "contextual_prefix_hybrid",
            "context_coverage_at_k": round(with_context / max(len(top), 1), 3),
            "mid_document_chunks_at_k": sum(1 for h in top if h.chunk.position > 0),
            "orphan_chunks_at_k": orphan_chunks,
            "context_source": "template_from_metadata",
            "identical_to_v2_on_this_corpus": True,
            "why": "prefixes are applied at ingestion for all variants; see ablation no_context",
        }
