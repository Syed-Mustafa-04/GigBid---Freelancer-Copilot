"""Embeddings and reranking - open models in both profiles.

Three backends, chosen by ``EMBEDDING_BACKEND``:

``local``   sentence-transformers (BAAI/bge-small-en-v1.5). The default and
            what the reported numbers are measured with. Runs on CPU.
``hosted``  any OpenAI-compatible ``/embeddings`` endpoint, for the GPU-less
            deployment host.
``hash``    a deterministic hashed character-n-gram projection. No download, no
            network, no model. It is a *real* retrieval signal - roughly a
            random-projected character n-gram TF-IDF - but it is markedly
            weaker than a trained encoder, and anything measured with it is
            tagged ``embedding_backend: hash`` in the results file so it can
            never be mistaken for a measured bge number.

The reranker is a cross-encoder in the same spirit, with an LLM-scored and a
no-op fallback.
"""

from __future__ import annotations

import hashlib
import os
import re
from abc import ABC, abstractmethod
from typing import Any

import numpy as np

TOKEN_RE = re.compile(r"[a-z0-9]+")


def _l2(matrix: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(matrix, axis=-1, keepdims=True)
    return matrix / np.clip(norms, 1e-9, None)


class Embedder(ABC):
    name: str = "base"
    dimensions: int = 384

    @abstractmethod
    def encode(self, texts: list[str], *, is_query: bool = False) -> np.ndarray: ...

    def encode_one(self, text: str, *, is_query: bool = False) -> np.ndarray:
        return self.encode([text], is_query=is_query)[0]


class HashEmbedder(Embedder):
    """Deterministic offline embedder: hashed word + character n-grams.

    Not a trained model. Included so the pipeline is runnable with no download
    and so CI measures the same code path. Sub-word n-grams give it partial
    robustness to morphology, but it has no semantics: "matrix factorisation"
    and "collaborative filtering" stay far apart, which is exactly the gap the
    corpus is designed to probe.
    """

    name = "hash"

    def __init__(self, dimensions: int = 384, char_ngram: int = 4) -> None:
        self.dimensions = dimensions
        self.char_ngram = char_ngram

    def _features(self, text: str) -> dict[str, float]:
        lowered = text.lower()
        words = TOKEN_RE.findall(lowered)
        feats: dict[str, float] = {}
        for word in words:
            feats[f"w:{word}"] = feats.get(f"w:{word}", 0.0) + 1.0
        for i in range(len(words) - 1):
            bigram = f"b:{words[i]}_{words[i + 1]}"
            feats[bigram] = feats.get(bigram, 0.0) + 0.7
        joined = " ".join(words)
        n = self.char_ngram
        for i in range(max(0, len(joined) - n + 1)):
            gram = f"c:{joined[i : i + n]}"
            feats[gram] = feats.get(gram, 0.0) + 0.25
        return feats

    def encode(self, texts: list[str], *, is_query: bool = False) -> np.ndarray:
        out = np.zeros((len(texts), self.dimensions), dtype=np.float32)
        for row, text in enumerate(texts):
            for feature, weight in self._features(text).items():
                digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
                index = int.from_bytes(digest[:4], "little") % self.dimensions
                sign = 1.0 if digest[4] & 1 else -1.0
                # Sub-linear term weighting, as in TF-IDF.
                out[row, index] += sign * (1.0 + np.log(weight))
        return _l2(out)


class LocalEmbedder(Embedder):
    """sentence-transformers on CPU. The default for reported numbers."""

    name = "local"

    def __init__(self, model_name: str = "BAAI/bge-small-en-v1.5", batch_size: int = 32) -> None:
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "EMBEDDING_BACKEND=local needs sentence-transformers: "
                'pip install -e ".[embed]"  (or set EMBEDDING_BACKEND=hash)'
            ) from exc
        self.model = SentenceTransformer(model_name)
        self.model_name = model_name
        self.batch_size = batch_size
        self.dimensions = int(self.model.get_sentence_embedding_dimension())

    def encode(self, texts: list[str], *, is_query: bool = False) -> np.ndarray:
        # BGE models want an instruction prefix on the query side only.
        prepared = [f"Represent this sentence for searching relevant passages: {t}" for t in texts] if is_query else texts
        vectors = self.model.encode(
            prepared,
            batch_size=self.batch_size,
            convert_to_numpy=True,
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        return vectors.astype(np.float32)


class HostedEmbedder(Embedder):
    """Any OpenAI-compatible /embeddings endpoint serving an open model."""

    name = "hosted"

    def __init__(self, base_url: str, api_key: str, model: str, dimensions: int = 384) -> None:
        import httpx

        self.base_url = base_url.rstrip("/")
        self.model = model
        self.dimensions = dimensions
        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=60.0,
            headers={"Authorization": f"Bearer {api_key}"},
        )

    def encode(self, texts: list[str], *, is_query: bool = False) -> np.ndarray:
        vectors: list[list[float]] = []
        for start in range(0, len(texts), 64):
            batch = texts[start : start + 64]
            response = self._client.post(
                "/embeddings", json={"model": self.model, "input": batch}
            )
            response.raise_for_status()
            payload = response.json()
            vectors.extend(item["embedding"] for item in payload["data"])
        matrix = np.asarray(vectors, dtype=np.float32)
        self.dimensions = matrix.shape[1]
        return _l2(matrix)


def get_embedder(config: Any | None = None) -> Embedder:
    from agent.config import get_config

    cfg = config or get_config()
    spec = cfg.section("models.embeddings")
    backend = os.getenv(spec.get("backend_env", "EMBEDDING_BACKEND"), "local").lower()

    if backend == "hash":
        return HashEmbedder(dimensions=int(spec.get("dimensions", 384)))
    if backend == "hosted":
        base = os.getenv("EMBEDDING_HOSTED_BASE_URL", "")
        key = os.getenv("EMBEDDING_HOSTED_API_KEY", "")
        if not base or not key:
            return HashEmbedder(dimensions=int(spec.get("dimensions", 384)))
        return HostedEmbedder(base, key, spec.get("hosted_model", "BAAI/bge-large-en-v1.5"))
    try:
        return LocalEmbedder(
            spec.get("local_model", "BAAI/bge-small-en-v1.5"),
            int(spec.get("batch_size", 32)),
        )
    except RuntimeError:
        # Degrade visibly rather than failing the whole system.
        print("[rag] sentence-transformers unavailable; falling back to the hash embedder.")
        return HashEmbedder(dimensions=int(spec.get("dimensions", 384)))


# ---------------------------------------------------------------------------
# Reranking
# ---------------------------------------------------------------------------
class Reranker(ABC):
    name = "base"

    @abstractmethod
    def rank(self, query: str, documents: list[str]) -> list[float]: ...


class NoopReranker(Reranker):
    name = "noop"

    def rank(self, query: str, documents: list[str]) -> list[float]:
        return [0.0] * len(documents)


class CrossEncoderReranker(Reranker):
    name = "cross_encoder"

    def __init__(self, model_name: str = "BAAI/bge-reranker-base") -> None:
        from sentence_transformers import CrossEncoder

        self.model = CrossEncoder(model_name, max_length=512)
        self.model_name = model_name

    def rank(self, query: str, documents: list[str]) -> list[float]:
        if not documents:
            return []
        pairs = [(query, doc[:2000]) for doc in documents]
        scores = self.model.predict(pairs, show_progress_bar=False)
        return [float(s) for s in scores]


class LexicalOverlapReranker(Reranker):
    """Offline stand-in for a cross-encoder.

    Scores IDF-weighted term overlap plus a coverage bonus for queries whose
    terms are *all* present. Weaker than a trained cross-encoder, but it is a
    genuine precision signal rather than a no-op, so V3 remains meaningful when
    torch is unavailable. Tagged in results as ``reranker: lexical``.
    """

    name = "lexical"

    def rank(self, query: str, documents: list[str]) -> list[float]:
        q_terms = set(TOKEN_RE.findall(query.lower()))
        if not q_terms:
            return [0.0] * len(documents)
        doc_terms = [set(TOKEN_RE.findall(d.lower())) for d in documents]
        n_docs = max(len(documents), 1)
        df = {t: sum(1 for dt in doc_terms if t in dt) for t in q_terms}
        scores: list[float] = []
        for terms in doc_terms:
            hit = q_terms & terms
            if not hit:
                scores.append(0.0)
                continue
            idf_sum = sum(np.log(1 + n_docs / (1 + df[t])) for t in hit)
            coverage = len(hit) / len(q_terms)
            scores.append(float(idf_sum * (0.5 + 0.5 * coverage)))
        return scores


def get_reranker(config: Any | None = None) -> Reranker:
    from agent.config import get_config

    cfg = config or get_config()
    spec = cfg.section("models.reranker")
    if not spec.get("enabled", True):
        return NoopReranker()
    backend = spec.get("backend", "local")
    if backend == "none":
        return NoopReranker()
    if backend == "local":
        try:
            return CrossEncoderReranker(spec.get("local_model", "BAAI/bge-reranker-base"))
        except Exception:  # noqa: BLE001
            print("[rag] cross-encoder unavailable; using the lexical reranker.")
            return LexicalOverlapReranker()
    return LexicalOverlapReranker()
