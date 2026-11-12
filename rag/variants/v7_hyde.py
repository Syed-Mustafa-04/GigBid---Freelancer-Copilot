"""V7 - HyDE: retrieve with a hypothetical ideal proposal, not the question.

A vague client post ("we need help with our data, it's a mess, not sure where
to start") shares almost no vocabulary with the portfolio evidence that would
answer it. The query and the documents live in different registers: one is a
problem statement, the other is a delivery write-up.

HyDE closes that gap by having the model write the answer it *expects* to
find - a short hypothetical case study - and embedding that instead. The
hypothetical is never shown to the user and never cited; it is a query
expansion device, and every claim in the final proposal is still grounded in
real retrieved evidence by the verifier.

Costs one small LLM call per job. The ablation reports whether that call pays
for itself, and it is expected to pay off only on the vague end of the corpus.
"""

from __future__ import annotations

from typing import Any

from rag.index.store import CorpusIndex, Hit
from rag.variants.base import RetrievalQuery, RetrievalVariant

HYDE_SYSTEM = (
    "You write a short, plausible portfolio case study that would be the ideal "
    "evidence for the job described. Write it as a freelancer describing work "
    "they already delivered: what the client needed, what was built, which "
    "tools were used, and the measurable result. Three to five sentences. "
    "Name concrete technologies. Do not address the client and do not offer "
    "to do the work - this is a description of past work, not a proposal."
)


class V7HyDE(RetrievalVariant):
    id = "v7_hyde"
    group = "query"
    description = "Hypothetical document embedding: retrieve with a generated ideal case study"

    def __init__(self, index: CorpusIndex, params: dict[str, Any] | None = None) -> None:
        super().__init__(index, params)
        self._llm = None

    @property
    def llm(self):
        if self._llm is None:
            from agent.llm import get_llm_client

            self._llm = get_llm_client()
        return self._llm

    async def _hypothetical(self, query: RetrievalQuery) -> tuple[str, int, int]:
        prompt = (
            f"Job post:\n{query.text[:1800]}\n\n"
            f"Required skills: {', '.join(query.skills) or 'unspecified'}\n"
            f"Requirements:\n" + "\n".join(f"- {r}" for r in query.requirements[:6])
        )
        try:
            result = await self.llm.complete(
                "analyst",
                [
                    {"role": "system", "content": HYDE_SYSTEM},
                    {"role": "user", "content": prompt},
                ],
            )
            return result.text, 1, result.usage.total
        except Exception:  # noqa: BLE001 - never let query expansion break retrieval
            return "", 0, 0

    async def _retrieve(self, query: RetrievalQuery) -> tuple[list[Hit], dict[str, Any]]:
        fetch_k = int(self.params.get("fetch_k", 40))
        rrf_k = int(self.params.get("rrf_k", 60))

        hypothetical, calls, tokens = await self._hypothetical(query)
        original = query.as_single_string()

        rankings = []
        if hypothetical:
            rankings.append(self._dense(hypothetical, fetch_k, query))
        # Always keep the literal query in the fusion: HyDE can hallucinate a
        # technology the client never asked for, and the real post anchors it.
        rankings.append(self._dense(original, fetch_k, query))
        rankings.append(self._lexical(original, fetch_k, query))

        merged = self._rrf(rankings, rrf_k=rrf_k, limit=fetch_k)
        return merged, {
            "strategy": "hyde_plus_literal_rrf",
            "hypothetical_generated": bool(hypothetical),
            "hypothetical_preview": hypothetical[:220],
            "hypothetical_words": len(hypothetical.split()),
            "llm_calls": calls,
            "llm_tokens": tokens,
        }
