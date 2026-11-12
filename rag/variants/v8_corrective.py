"""V8 - corrective RAG as a LangGraph loop: grade, re-query, or refuse.

Every other variant returns its top-k unconditionally. On a job the persona
genuinely cannot serve, that is actively harmful: it hands the Writer eight
weakly-related fragments, and a model given weak evidence and asked to write a
confident proposal will stretch them. That is the mechanism behind most
unsupported claims.

This variant adds the missing step - judging whether what came back is good
enough - as a real graph with a cycle:

    retrieve -> grade -> (requery -> retrieve)* -> finalise

and three terminal outcomes:

* **sufficient** - enough strong evidence, proceed;
* **requery** - weak evidence but a reformulation is worth trying (bounded by
  ``max_requeries``, so the cycle always terminates);
* **insufficient** - nothing graded strong enough. It returns what little it
  has with ``insufficient_evidence: True``, which the decision node reads as a
  reason to skip the job and the Writer reads as a prohibition on claiming.

Refusing to claim is the point. "I have no evidence for this" is a correct
answer that no top-k retriever can produce.
"""

from __future__ import annotations

from typing import Annotated, Any, TypedDict

from rag.index.store import CorpusIndex, Hit
from rag.variants.base import RetrievalQuery, RetrievalVariant


def _keep_last(_: Any, new: Any) -> Any:
    return new


class CRAGState(TypedDict, total=False):
    query_text: Annotated[str, _keep_last]
    candidates: Annotated[list[Hit], _keep_last]
    graded: Annotated[list[tuple[Hit, float]], _keep_last]
    requeries: Annotated[int, _keep_last]
    verdict: Annotated[str, _keep_last]
    trace: Annotated[list[dict[str, Any]], _keep_last]
    # The originating query travels in state so nodes stay pure functions of it.
    query: Annotated[Any, _keep_last]


class V8Corrective(RetrievalVariant):
    id = "v8_corrective"
    group = "agentic"
    description = "Corrective RAG: grade retrieved evidence, re-query when weak, refuse when none"

    def __init__(self, index: CorpusIndex, params: dict[str, Any] | None = None) -> None:
        super().__init__(index, params)
        self._graph = None

    # -- grading -----------------------------------------------------------
    def _grade(self, query: RetrievalQuery, hit: Hit) -> float:
        """Cheap, deterministic relevance grade in [0, 1].

        Lexical overlap against requirements and skills rather than an LLM
        call per candidate: grading 40 candidates with a model would cost more
        than the whole rest of the pipeline, and the grade only has to be good
        enough to separate "clearly on topic" from "clearly not".
        """
        text = f"{hit.chunk.title} {hit.chunk.context_prefix} {hit.chunk.text}".lower()
        terms: set[str] = set()
        for source in (query.skills, query.requirements, [query.text]):
            for entry in source:
                terms.update(w for w in entry.lower().split() if len(w) > 3)
        if not terms:
            return 0.5
        overlap = sum(1 for t in terms if t in text) / len(terms)

        skill_hit = 0.0
        if query.skills:
            chunk_skills = {s.lower() for s in hit.chunk.skills}
            wanted = {s.lower() for s in query.skills}
            if chunk_skills & wanted:
                skill_hit = 0.3
            elif any(w in text for w in wanted):
                skill_hit = 0.2

        industry_hit = 0.1 if (
            query.industry and hit.chunk.client_industry
            and query.industry.lower() == hit.chunk.client_industry.lower()
        ) else 0.0

        return float(min(1.0, 0.6 * overlap + skill_hit + industry_hit))

    def _rewrite(self, query: RetrievalQuery, attempt: int) -> str:
        """Broaden the query without an LLM call: drop to core skills, then title."""
        if attempt == 0 and query.skills:
            return " ".join(query.skills) + " project delivered outcome"
        if query.requirements:
            return " ".join(query.requirements[:2])
        return query.text[:300]

    # -- graph -------------------------------------------------------------
    def _build_graph(self):
        from langgraph.graph import END, START, StateGraph

        threshold = float(self.params.get("grade_threshold", 0.5))
        max_requeries = int(self.params.get("max_requeries", 2))
        fetch_k = int(self.params.get("fetch_k", 40))
        rrf_k = int(self.params.get("rrf_k", 60))
        variant = self

        async def retrieve(state: CRAGState) -> dict[str, Any]:
            text = state["query_text"]
            q: RetrievalQuery = state["query"]
            dense = variant._dense(text, fetch_k, q)
            lexical = variant._lexical(text, fetch_k, q)
            merged = variant._rrf([dense, lexical], rrf_k=rrf_k, limit=fetch_k)
            trace = state.get("trace", []) + [
                {"node": "retrieve", "query": text[:120], "candidates": len(merged)}
            ]
            return {"candidates": merged, "trace": trace}

        async def grade(state: CRAGState) -> dict[str, Any]:
            q: RetrievalQuery = state["query"]
            graded = [(hit, variant._grade(q, hit)) for hit in state.get("candidates", [])]
            graded.sort(key=lambda pair: pair[1], reverse=True)
            strong = [g for g in graded if g[1] >= threshold]

            if len(strong) >= max(3, q.k // 2):
                verdict = "sufficient"
            elif state.get("requeries", 0) < max_requeries:
                verdict = "requery"
            elif strong:
                verdict = "sufficient"
            else:
                verdict = "insufficient"

            trace = state.get("trace", []) + [
                {
                    "node": "grade",
                    "graded": len(graded),
                    "strong": len(strong),
                    "best": round(graded[0][1], 3) if graded else 0.0,
                    "verdict": verdict,
                }
            ]
            return {"graded": graded, "verdict": verdict, "trace": trace}

        async def requery(state: CRAGState) -> dict[str, Any]:
            q: RetrievalQuery = state["query"]
            attempt = state.get("requeries", 0)
            new_text = variant._rewrite(q, attempt)
            trace = state.get("trace", []) + [
                {"node": "requery", "attempt": attempt + 1, "new_query": new_text[:120]}
            ]
            return {"query_text": new_text, "requeries": attempt + 1, "trace": trace}

        def route(state: CRAGState) -> str:
            return "requery" if state.get("verdict") == "requery" else "done"

        builder = StateGraph(CRAGState)
        builder.add_node("retrieve", retrieve)
        builder.add_node("grade", grade)
        builder.add_node("requery", requery)
        builder.add_edge(START, "retrieve")
        builder.add_edge("retrieve", "grade")
        builder.add_conditional_edges("grade", route, {"requery": "requery", "done": END})
        builder.add_edge("requery", "retrieve")
        return builder.compile()

    @property
    def graph(self):
        if self._graph is None:
            self._graph = self._build_graph()
        return self._graph

    async def _retrieve(self, query: RetrievalQuery) -> tuple[list[Hit], dict[str, Any]]:
        threshold = float(self.params.get("grade_threshold", 0.5))
        initial: dict[str, Any] = {
            "query_text": query.as_single_string(),
            "requeries": 0,
            "trace": [],
            "query": query,
        }
        final = await self.graph.ainvoke(initial)

        graded: list[tuple[Hit, float]] = final.get("graded", [])
        verdict = final.get("verdict", "insufficient")
        hits = [
            Hit(
                chunk=hit.chunk,
                score=score,
                retrieved_by="corrective",
                rank=rank,
                components={**hit.components, "grade": round(score, 3)},
            )
            for rank, (hit, score) in enumerate(graded)
        ]
        if verdict == "insufficient":
            # Hand back only what passed, which may be nothing. Returning
            # weak evidence anyway is the failure mode this variant exists to
            # prevent.
            hits = [h for h in hits if h.score >= threshold]

        return hits, {
            "strategy": "corrective_rag_graph",
            "verdict": verdict,
            "insufficient_evidence": verdict == "insufficient",
            "requeries": final.get("requeries", 0),
            "graded_candidates": len(graded),
            "strong_candidates": sum(1 for _, s in graded if s >= threshold),
            "grade_threshold": threshold,
            "graph_trace": final.get("trace", []),
        }
