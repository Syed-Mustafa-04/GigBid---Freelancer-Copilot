"""Common contract for retrieval variants (B2).

Every variant takes the same :class:`RetrievalQuery` and returns the same
:class:`RetrievalResult`, so ``rag_eval.py`` can swap one for another with a
config key and nothing else changes. The ``diagnostics`` dict is where a
variant explains what it did - how many sub-queries it issued, what the
reranker changed, which requeries fired - and that is what the ablation
report is written from.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from agent.handoffs import EvidenceItem
from rag.index.store import CorpusIndex, Hit


@dataclass
class RetrievalQuery:
    """What the Fit Matcher or Proposal Writer is asking for."""

    text: str
    requirements: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    persona_id: str | None = None
    industry: str | None = None
    k: int = 8
    job_id: str = ""

    def as_single_string(self) -> str:
        parts = [self.text]
        if self.requirements:
            parts.append(" ".join(self.requirements))
        if self.skills:
            parts.append(" ".join(self.skills))
        return "\n".join(p for p in parts if p)


@dataclass
class RetrievalResult:
    hits: list[Hit]
    variant: str
    latency_s: float = 0.0
    diagnostics: dict[str, Any] = field(default_factory=dict)
    llm_calls: int = 0
    llm_tokens: int = 0

    @property
    def evidence_ids(self) -> list[str]:
        """Unique evidence ids in rank order."""
        seen: list[str] = []
        for hit in self.hits:
            if hit.chunk.evidence_id not in seen:
                seen.append(hit.chunk.evidence_id)
        return seen

    @property
    def project_ids(self) -> list[str]:
        seen: list[str] = []
        for hit in self.hits:
            pid = hit.chunk.project_id
            if pid and pid not in seen:
                seen.append(pid)
        return seen

    def to_evidence_items(self) -> list[EvidenceItem]:
        """Convert to the hand-off type the graph passes around."""
        items: list[EvidenceItem] = []
        seen: set[str] = set()
        for hit in self.hits:
            chunk = hit.chunk
            if chunk.evidence_id in seen:
                continue
            seen.add(chunk.evidence_id)
            items.append(
                EvidenceItem(
                    evidence_id=chunk.evidence_id,
                    freelancer_id=chunk.persona_id,
                    type=chunk.type if chunk.type in
                    {"case_study", "repo", "code", "readme", "review", "proposal", "cv"} else "case_study",
                    title=chunk.title,
                    text=chunk.text,
                    skills=chunk.skills,
                    project_id=chunk.project_id,
                    client_industry=chunk.client_industry,
                    date=chunk.date,
                    outcome=chunk.outcome if chunk.outcome in
                    {"won", "replied", "ignored", "lost", "delivered", "unknown"} else "unknown",
                    source=chunk.source,
                    score=hit.score,
                    retrieved_by=hit.retrieved_by,
                )
            )
        return items


class RetrievalVariant(ABC):
    """One retrieval design. Configured, never hard-coded."""

    id: str = "base"
    group: str = "baseline"
    description: str = ""

    def __init__(self, index: CorpusIndex, params: dict[str, Any] | None = None) -> None:
        self.index = index
        self.params = params or {}

    @abstractmethod
    async def _retrieve(self, query: RetrievalQuery) -> tuple[list[Hit], dict[str, Any]]: ...

    async def retrieve(self, query: RetrievalQuery) -> RetrievalResult:
        started = time.perf_counter()
        hits, diagnostics = await self._retrieve(query)
        return RetrievalResult(
            hits=hits[: query.k],
            variant=self.id,
            latency_s=time.perf_counter() - started,
            diagnostics=diagnostics,
            llm_calls=int(diagnostics.get("llm_calls", 0)),
            llm_tokens=int(diagnostics.get("llm_tokens", 0)),
        )

    # -- shared helpers ----------------------------------------------------
    def _dense(self, text: str, k: int, query: RetrievalQuery) -> list[Hit]:
        vector = self.index.embed_query(text)
        return self.index.vectors.search(
            vector, k=k, allowed=self.index.persona_filter(query.persona_id)
        )

    def _lexical(self, text: str, k: int, query: RetrievalQuery) -> list[Hit]:
        if self.index.bm25 is None:
            return []
        hits = self.index.bm25.search(text, k=k * 3)
        allowed = self.index.persona_filter(query.persona_id)
        if allowed is None:
            return hits[:k]
        positions = {id(c): i for i, c in enumerate(self.index.vectors.chunks)}
        return [h for h in hits if positions.get(id(h.chunk), -1) in allowed][:k]

    @staticmethod
    def _rrf(rankings: list[list[Hit]], rrf_k: int = 60, limit: int = 40) -> list[Hit]:
        """Reciprocal rank fusion over several ranked lists."""
        scores: dict[str, float] = {}
        best: dict[str, Hit] = {}
        parts: dict[str, dict[str, float]] = {}
        for ranking in rankings:
            for rank, hit in enumerate(ranking):
                key = hit.chunk.chunk_id
                contribution = 1.0 / (rrf_k + rank + 1)
                scores[key] = scores.get(key, 0.0) + contribution
                parts.setdefault(key, {})[hit.retrieved_by or "unknown"] = contribution
                if key not in best or hit.score > best[key].score:
                    best[key] = hit
        ordered = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[:limit]
        out: list[Hit] = []
        for rank, (key, score) in enumerate(ordered):
            hit = best[key]
            out.append(
                Hit(
                    chunk=hit.chunk,
                    score=score,
                    retrieved_by="rrf",
                    rank=rank,
                    components=parts.get(key, {}),
                )
            )
        return out

    @staticmethod
    def _dedupe_by_project(hits: list[Hit], limit: int) -> list[Hit]:
        """Keep the best chunk per project so one project cannot fill top-k."""
        seen: set[str] = set()
        out: list[Hit] = []
        for hit in hits:
            key = hit.chunk.project_id or hit.chunk.evidence_id
            if key in seen:
                continue
            seen.add(key)
            out.append(hit)
            if len(out) >= limit:
                break
        return out
