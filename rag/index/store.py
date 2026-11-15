"""The index: dense vectors, BM25, and a skill graph.

One ``CorpusIndex`` holds all three views of the same chunks so a variant can
pick whichever it needs without re-ingesting:

* **dense** - normalised embeddings, cosine similarity via a single matmul.
  A hand-rolled numpy store rather than an external vector database: the
  corpus is ~3k chunks, a matmul over it is sub-millisecond, and it removes a
  service from ``docker compose`` without changing any retrieval semantics.
  ``QdrantStore`` is behind the same interface for when that stops being true.
* **lexical** - BM25 over the same chunks. Carries exact tool and library
  names ("Celery", "dbt") that embeddings blur.
* **graph** - skills, projects, clients and outcomes as nodes, for V9.
"""

from __future__ import annotations

import json
import pickle
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from rag.embeddings import TOKEN_RE, Embedder


@dataclass
class Chunk:
    """One indexed fragment. ``evidence_id`` is what a proposal claim cites."""

    chunk_id: str
    evidence_id: str
    persona_id: str
    project_id: str | None
    type: str
    view: str
    title: str
    text: str
    skills: list[str] = field(default_factory=list)
    client_industry: str | None = None
    date: str = ""
    outcome: str = "unknown"
    source: str = ""
    position: int = 0
    context_prefix: str = ""

    def embed_text(self) -> str:
        """What actually gets embedded - title and context carry signal."""
        parts = [self.title, self.context_prefix, self.text]
        return "\n".join(p for p in parts if p)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Hit:
    chunk: Chunk
    score: float
    retrieved_by: str = ""
    rank: int = 0
    components: dict[str, float] = field(default_factory=dict)


class IndexEmbedderMismatch(RuntimeError):
    """Raised when a persisted index was built by a different embedder.

    A distinct type so a caller can tell this apart from a corrupt or missing
    index: the remedy is a rebuild or a configuration change, not recovery.
    """


class VectorStore:
    """Dense store: a float32 matrix plus aligned chunk metadata."""

    def __init__(self, dimensions: int) -> None:
        self.dimensions = dimensions
        self.matrix: np.ndarray = np.zeros((0, dimensions), dtype=np.float32)
        self.chunks: list[Chunk] = []
        self._by_id: dict[str, int] = {}

    def add(self, chunks: list[Chunk], vectors: np.ndarray) -> None:
        if len(chunks) != vectors.shape[0]:
            raise ValueError("chunk/vector count mismatch")
        start = len(self.chunks)
        self.matrix = np.vstack([self.matrix, vectors.astype(np.float32)]) if len(self.chunks) else vectors.astype(np.float32)
        self.chunks.extend(chunks)
        for offset, chunk in enumerate(chunks):
            self._by_id[chunk.chunk_id] = start + offset

    def search(
        self, query_vector: np.ndarray, k: int = 10, *, allowed: set[int] | None = None
    ) -> list[Hit]:
        if not self.chunks:
            return []
        scores = self.matrix @ query_vector.astype(np.float32)
        if allowed is not None:
            mask = np.full(scores.shape, -np.inf, dtype=np.float32)
            idx = np.fromiter(allowed, dtype=np.int64, count=len(allowed))
            if idx.size:
                mask[idx] = scores[idx]
            scores = mask
        k = min(k, len(self.chunks))
        top = np.argpartition(-scores, k - 1)[:k]
        top = top[np.argsort(-scores[top])]
        return [
            Hit(chunk=self.chunks[i], score=float(scores[i]), retrieved_by="dense", rank=rank)
            for rank, i in enumerate(top)
            if np.isfinite(scores[i])
        ]

    def get(self, chunk_id: str) -> Chunk | None:
        idx = self._by_id.get(chunk_id)
        return self.chunks[idx] if idx is not None else None

    def __len__(self) -> int:
        return len(self.chunks)


class BM25Index:
    """Lexical index. Falls back to a self-contained BM25 if rank_bm25 is absent."""

    def __init__(self, chunks: list[Chunk]) -> None:
        self.chunks = chunks
        self.corpus = [TOKEN_RE.findall(c.embed_text().lower()) for c in chunks]
        try:
            from rank_bm25 import BM25Okapi

            self._impl = BM25Okapi(self.corpus)
            self._native = True
        except ImportError:  # pragma: no cover
            self._impl = None
            self._native = False
            self._build_fallback()

    def _build_fallback(self) -> None:
        self.df: dict[str, int] = defaultdict(int)
        for doc in self.corpus:
            for term in set(doc):
                self.df[term] += 1
        self.avgdl = sum(len(d) for d in self.corpus) / max(len(self.corpus), 1)
        self.n = len(self.corpus)

    def scores(self, query: str) -> np.ndarray:
        terms = TOKEN_RE.findall(query.lower())
        if not terms or not self.corpus:
            return np.zeros(len(self.corpus), dtype=np.float32)
        if self._native:
            return np.asarray(self._impl.get_scores(terms), dtype=np.float32)
        k1, b = 1.5, 0.75
        out = np.zeros(self.n, dtype=np.float32)
        for i, doc in enumerate(self.corpus):
            dl = len(doc) or 1
            freqs: dict[str, int] = defaultdict(int)
            for token in doc:
                freqs[token] += 1
            total = 0.0
            for term in terms:
                tf = freqs.get(term, 0)
                if not tf:
                    continue
                idf = np.log(1 + (self.n - self.df[term] + 0.5) / (self.df[term] + 0.5))
                total += idf * (tf * (k1 + 1)) / (tf + k1 * (1 - b + b * dl / self.avgdl))
            out[i] = total
        return out

    def search(self, query: str, k: int = 10) -> list[Hit]:
        scores = self.scores(query)
        if not len(scores):
            return []
        k = min(k, len(scores))
        top = np.argpartition(-scores, k - 1)[:k]
        top = top[np.argsort(-scores[top])]
        return [
            Hit(chunk=self.chunks[i], score=float(scores[i]), retrieved_by="bm25", rank=rank)
            for rank, i in enumerate(top)
            if scores[i] > 0
        ]


class SkillGraph:
    """Skills, projects, clients and outcomes as a traversable graph (V9).

    Answers the shape of question embeddings handle badly: "have I done X for a
    client in industry Y?" - which is a two-hop join, not a similarity.
    """

    def __init__(self) -> None:
        self.skill_to_projects: dict[str, set[str]] = defaultdict(set)
        self.project_to_skills: dict[str, set[str]] = defaultdict(set)
        self.project_to_evidence: dict[str, set[str]] = defaultdict(set)
        self.industry_to_projects: dict[str, set[str]] = defaultdict(set)
        self.project_outcomes: dict[str, list[str]] = defaultdict(list)
        self.skill_cooccurrence: dict[tuple[str, str], int] = defaultdict(int)
        self.project_meta: dict[str, dict[str, Any]] = {}

    def add_chunk(self, chunk: Chunk) -> None:
        if not chunk.project_id:
            return
        pid = chunk.project_id
        self.project_to_evidence[pid].add(chunk.evidence_id)
        skills = {s.lower() for s in chunk.skills if s}
        for skill in skills:
            self.skill_to_projects[skill].add(pid)
            self.project_to_skills[pid].add(skill)
        for a in skills:
            for b in skills:
                if a < b:
                    self.skill_cooccurrence[(a, b)] += 1
        if chunk.client_industry:
            self.industry_to_projects[chunk.client_industry.lower()].add(pid)
        if chunk.outcome and chunk.outcome != "unknown":
            self.project_outcomes[pid].append(chunk.outcome)

    def add_project_meta(self, project: dict[str, Any]) -> None:
        pid = project.get("project_id")
        if not pid:
            return
        self.project_meta[pid] = project
        primary = str(project.get("primary_skill", "")).lower()
        if primary:
            self.skill_to_projects[primary].add(pid)
            self.project_to_skills[pid].add(primary)
        industry = str(project.get("industry", "")).lower()
        if industry:
            self.industry_to_projects[industry].add(pid)

    def expand(self, skills: list[str], *, hops: int = 2, decay: float = 0.6) -> dict[str, float]:
        """Weighted project set reachable from the query's skills."""
        scores: dict[str, float] = defaultdict(float)
        frontier = {s.lower(): 1.0 for s in skills if s}
        seen: set[str] = set()
        for hop in range(max(1, hops)):
            nxt: dict[str, float] = defaultdict(float)
            weight = decay**hop
            for skill, strength in frontier.items():
                if skill in seen:
                    continue
                seen.add(skill)
                for pid in self.skill_to_projects.get(skill, set()):
                    scores[pid] += strength * weight
                    for neighbour in self.project_to_skills.get(pid, set()):
                        if neighbour not in seen:
                            nxt[neighbour] = max(nxt[neighbour], strength * decay)
            frontier = nxt
            if not frontier:
                break
        return dict(scores)

    def projects_for(self, skill: str, industry: str | None = None) -> set[str]:
        pool = set(self.skill_to_projects.get(skill.lower(), set()))
        if industry:
            pool &= self.industry_to_projects.get(industry.lower(), set())
        return pool

    def stats(self) -> dict[str, Any]:
        return {
            "skills": len(self.skill_to_projects),
            "projects": len(self.project_to_skills),
            "industries": len(self.industry_to_projects),
            "edges": sum(len(v) for v in self.skill_to_projects.values()),
        }


class CorpusIndex:
    """Everything a retrieval variant may need, built once."""

    def __init__(self, embedder: Embedder) -> None:
        self.embedder = embedder
        self.vectors = VectorStore(embedder.dimensions)
        self.bm25: BM25Index | None = None
        self.graph = SkillGraph()
        self.by_evidence: dict[str, list[Chunk]] = defaultdict(list)
        self.by_project: dict[str, list[Chunk]] = defaultdict(list)
        self.by_persona: dict[str, list[int]] = defaultdict(list)
        self.evidence_meta: dict[str, dict[str, Any]] = {}

    def build(self, chunks: list[Chunk], projects: list[dict[str, Any]] | None = None) -> None:
        if not chunks:
            raise ValueError("cannot build an index from zero chunks")
        texts = [c.embed_text() for c in chunks]
        vectors = self.embedder.encode(texts)
        self.vectors.add(chunks, vectors)
        self.bm25 = BM25Index(chunks)
        for position, chunk in enumerate(chunks):
            self.by_evidence[chunk.evidence_id].append(chunk)
            if chunk.project_id:
                self.by_project[chunk.project_id].append(chunk)
            self.by_persona[chunk.persona_id].append(position)
            self.graph.add_chunk(chunk)
            self.evidence_meta.setdefault(
                chunk.evidence_id,
                {
                    "evidence_id": chunk.evidence_id,
                    "title": chunk.title,
                    "type": chunk.type,
                    "project_id": chunk.project_id,
                    "outcome": chunk.outcome,
                    "persona_id": chunk.persona_id,
                    "skills": chunk.skills,
                    "client_industry": chunk.client_industry,
                    "date": chunk.date,
                    "source": chunk.source,
                },
            )
        for project in projects or []:
            self.graph.add_project_meta(project)

    def persona_filter(self, persona_id: str | None) -> set[int] | None:
        if not persona_id:
            return None
        return set(self.by_persona.get(persona_id, []))

    def embed_query(self, text: str) -> np.ndarray:
        return self.embedder.encode_one(text, is_query=True)

    def stats(self) -> dict[str, Any]:
        return {
            "chunks": len(self.vectors),
            "evidence_items": len(self.by_evidence),
            "projects": len(self.by_project),
            "personas": len(self.by_persona),
            "embedder": self.embedder.name,
            "dimensions": self.embedder.dimensions,
            "graph": self.graph.stats(),
        }

    # -- persistence -------------------------------------------------------
    def save(self, directory: Path) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        np.save(directory / "vectors.npy", self.vectors.matrix)
        with (directory / "chunks.json").open("w", encoding="utf-8") as handle:
            json.dump([c.to_dict() for c in self.vectors.chunks], handle)
        with (directory / "graph.pkl").open("wb") as handle:
            pickle.dump(self.graph, handle)
        with (directory / "meta.json").open("w", encoding="utf-8") as handle:
            json.dump(self.stats(), handle, indent=2)

    @classmethod
    def load(cls, directory: Path, embedder: Embedder) -> CorpusIndex:
        index = cls(embedder)

        # Refuse an index built by a different embedder.
        #
        # This check exists because the failure it prevents is silent. The hash
        # embedder and bge-small are both 384-dimensional, so loading a
        # bge-built index and then embedding queries with the hash backend
        # raises nothing at all: the vectors load, the query encodes, cosine
        # similarity computes, and every result is meaningless. Retrieval
        # quietly returns noise and the proposal writer grounds its claims in
        # it.
        #
        # It is easy to reach. The Docker image bakes the index at build time
        # and the backend is chosen by an environment variable at run time, so
        # any deployment that sets EMBEDDING_BACKEND without rebuilding lands
        # here. Better to refuse to start than to serve confident nonsense.
        meta_path = directory / "meta.json"
        if meta_path.exists():
            with meta_path.open("r", encoding="utf-8") as handle:
                meta = json.load(handle)
            built_with = meta.get("embedder")
            if built_with and built_with != embedder.name:
                raise IndexEmbedderMismatch(
                    f"This index was built with the {built_with!r} embedder but "
                    f"{embedder.name!r} is configured now. Both may share a "
                    f"dimension, so the mismatch would not otherwise be detected "
                    f"and retrieval would silently return noise. Rebuild the "
                    f"index with EMBEDDING_BACKEND={built_with}, or set "
                    f"EMBEDDING_BACKEND={built_with} to use the existing one."
                )

        matrix = np.load(directory / "vectors.npy")
        with (directory / "chunks.json").open("r", encoding="utf-8") as handle:
            chunks = [Chunk(**row) for row in json.load(handle)]
        index.vectors = VectorStore(matrix.shape[1])
        index.vectors.add(chunks, matrix)
        index.bm25 = BM25Index(chunks)
        for position, chunk in enumerate(chunks):
            index.by_evidence[chunk.evidence_id].append(chunk)
            if chunk.project_id:
                index.by_project[chunk.project_id].append(chunk)
            index.by_persona[chunk.persona_id].append(position)
            index.evidence_meta.setdefault(
                chunk.evidence_id,
                {
                    "evidence_id": chunk.evidence_id,
                    "title": chunk.title,
                    "type": chunk.type,
                    "project_id": chunk.project_id,
                    "outcome": chunk.outcome,
                    "persona_id": chunk.persona_id,
                    "skills": chunk.skills,
                    "client_industry": chunk.client_industry,
                    "date": chunk.date,
                    "source": chunk.source,
                },
            )
        graph_path = directory / "graph.pkl"
        if graph_path.exists():
            with graph_path.open("rb") as handle:
                index.graph = pickle.load(handle)
        return index
