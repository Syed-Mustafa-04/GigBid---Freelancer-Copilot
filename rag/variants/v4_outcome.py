"""V4 - outcome-aware ranking: boost evidence and phrasing that actually won.

Relevance is not the only thing that matters when the retrieved item is a past
proposal. Two proposals can be equally on-topic while one was won and the other
ignored, and the difference is in how it was written, not what it was about.

This variant applies a multiplicative prior from the recorded outcome, with a
recency decay so a win from 2022 counts for less than one from this year. It is
the only variant that can see the signal, because outcome is metadata rather
than text - cosine similarity is blind to it by construction.

The honest risk, which the ablation checks: outcome boosting can trade evidence
relevance for stylistic familiarity. If Recall@5 drops while reply rate rises,
that is a real result and it gets reported as one.
"""

from __future__ import annotations

import math
from datetime import date
from typing import Any

from rag.index.store import Hit
from rag.variants.base import RetrievalQuery, RetrievalVariant


class V4Outcome(RetrievalVariant):
    id = "v4_outcome"
    group = "ranking"
    description = "Outcome-aware ranking over hybrid candidates"

    def _recency(self, iso_date: str, half_life_days: float = 540.0) -> float:
        if not iso_date:
            return 0.6
        try:
            then = date.fromisoformat(iso_date[:10])
        except ValueError:
            return 0.6
        age = max((date(2026, 11, 1) - then).days, 0)
        return float(0.5 ** (age / half_life_days))

    async def _retrieve(self, query: RetrievalQuery) -> tuple[list[Hit], dict[str, Any]]:
        fetch_k = int(self.params.get("fetch_k", 40))
        rrf_k = int(self.params.get("rrf_k", 60))
        won_boost = float(self.params.get("won_boost", 0.25))
        replied_boost = float(self.params.get("replied_boost", 0.10))
        ignored_penalty = float(self.params.get("ignored_penalty", 0.15))
        text = query.as_single_string()

        dense = self._dense(text, fetch_k, query)
        lexical = self._lexical(text, fetch_k, query)
        candidates = self._rrf([dense, lexical], rrf_k=rrf_k, limit=fetch_k)

        adjustments: dict[str, int] = {"won": 0, "replied": 0, "ignored": 0, "neutral": 0}
        rescored: list[Hit] = []
        for hit in candidates:
            outcome = hit.chunk.outcome
            recency = self._recency(hit.chunk.date)
            multiplier = 1.0
            if outcome == "won":
                multiplier += won_boost * recency
                adjustments["won"] += 1
            elif outcome == "replied":
                multiplier += replied_boost * recency
                adjustments["replied"] += 1
            elif outcome == "ignored":
                multiplier -= ignored_penalty * recency
                adjustments["ignored"] += 1
            else:
                adjustments["neutral"] += 1
            # Delivered work with a client review is weak positive evidence.
            if hit.chunk.type == "review":
                multiplier += 0.05
            rescored.append(
                Hit(
                    chunk=hit.chunk,
                    score=hit.score * max(multiplier, 0.05),
                    retrieved_by="outcome_ranked",
                    rank=hit.rank,
                    components={**hit.components, "outcome_multiplier": round(multiplier, 3),
                                "recency": round(recency, 3)},
                )
            )

        rescored.sort(key=lambda h: h.score, reverse=True)
        for rank, hit in enumerate(rescored):
            hit.rank = rank

        top = rescored[: query.k]
        won_in_top = sum(1 for h in top if h.chunk.outcome == "won")
        return rescored, {
            "strategy": "hybrid_then_outcome_prior",
            "adjustments": adjustments,
            "won_in_topk": won_in_top,
            "won_share_at_k": round(won_in_top / max(len(top), 1), 3),
            "boost_entropy": round(
                -sum(
                    (v / max(sum(adjustments.values()), 1))
                    * math.log(max(v / max(sum(adjustments.values()), 1), 1e-9))
                    for v in adjustments.values()
                ),
                3,
            ),
        }
