"""V9 - GraphRAG over a skill graph: skills, projects, clients, outcomes.

Some questions a client asks are joins, not similarities:

    "Have you done X for a client in industry Y?"

Vector search answers that badly. A case study about X in a *different*
industry and one about something adjacent in industry Y both look similar to
the query, and neither is the answer. The structure - which project, which
skill, which industry, which outcome - is what the question is actually about,
and embeddings flatten exactly that structure away.

This variant walks the graph instead. Query skills seed a traversal that
expands through co-occurring skills (decayed per hop, so second-order matches
count for less), filters by industry when asked, then pulls the evidence
belonging to the surviving projects and orders it by graph weight with a
dense tiebreak.

Where it loses: a job whose skills were not extracted cleanly gives the
traversal nothing to start from, and it falls back to dense. That failure is
in the ablation.
"""

from __future__ import annotations

from typing import Any

from rag.index.store import Hit
from rag.variants.base import RetrievalQuery, RetrievalVariant


class V9SkillGraph(RetrievalVariant):
    id = "v9_skillgraph"
    group = "agentic"
    description = "GraphRAG: traverse skills -> projects -> evidence, filtered by industry"

    async def _retrieve(self, query: RetrievalQuery) -> tuple[list[Hit], dict[str, Any]]:
        hops = int(self.params.get("hops", 2))
        decay = float(self.params.get("edge_weight_decay", 0.6))
        graph = self.index.graph

        seeds = [s for s in query.skills if s]
        if not seeds:
            # Recover skills from the query text against known graph vocabulary.
            lowered = query.as_single_string().lower()
            seeds = [s for s in graph.skill_to_projects if s in lowered][:6]

        if not seeds:
            fallback = self._dense(query.as_single_string(), query.k * 3, query)
            return fallback, {
                "strategy": "graph_walk",
                "fell_back_to_dense": True,
                "reason": "no skill seeds could be resolved from the query",
                "seeds": [],
            }

        project_scores = graph.expand(seeds, hops=hops, decay=decay)

        industry_filtered = 0
        if query.industry:
            in_industry = graph.industry_to_projects.get(query.industry.lower(), set())
            if in_industry:
                boosted = {}
                for pid, score in project_scores.items():
                    boosted[pid] = score * (1.6 if pid in in_industry else 1.0)
                    if pid in in_industry:
                        industry_filtered += 1
                project_scores = boosted

        # Outcome prior from the graph: projects whose proposals won count more.
        for pid in list(project_scores):
            outcomes = graph.project_outcomes.get(pid, [])
            if outcomes:
                wins = sum(1 for o in outcomes if o == "won")
                project_scores[pid] *= 1.0 + 0.2 * (wins / len(outcomes))

        ranked_projects = sorted(project_scores.items(), key=lambda kv: kv[1], reverse=True)

        # Dense scores over the whole query provide the within-project tiebreak.
        dense = {h.chunk.chunk_id: h.score for h in self._dense(query.as_single_string(), 200, query)}

        hits: list[Hit] = []
        for pid, graph_score in ranked_projects[: max(query.k * 3, 24)]:
            chunks = self.index.by_project.get(pid, [])
            if not chunks:
                continue
            if query.persona_id:
                chunks = [c for c in chunks if c.persona_id == query.persona_id]
                if not chunks:
                    continue
            best = max(chunks, key=lambda c: dense.get(c.chunk_id, 0.0))
            hits.append(
                Hit(
                    chunk=best,
                    score=float(graph_score) + 0.25 * dense.get(best.chunk_id, 0.0),
                    retrieved_by="skillgraph",
                    rank=0,
                    components={
                        "graph_score": round(float(graph_score), 4),
                        "dense_tiebreak": round(dense.get(best.chunk_id, 0.0), 4),
                        "project_id": pid,
                        "project_skills": len(graph.project_to_skills.get(pid, set())),
                    },
                )
            )

        hits.sort(key=lambda h: h.score, reverse=True)
        for rank, hit in enumerate(hits):
            hit.rank = rank

        return hits, {
            "strategy": "graph_walk",
            "fell_back_to_dense": False,
            "seeds": seeds,
            "hops": hops,
            "projects_reached": len(project_scores),
            "industry_matches": industry_filtered,
            "graph_stats": graph.stats(),
        }
