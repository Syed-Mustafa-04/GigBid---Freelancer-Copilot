"""A persisted index must refuse an embedder it was not built with.

The failure this guards against is silent, which is why it is worth a test.
HashEmbedder and bge-small are both 384-dimensional, so loading a bge-built
index while the hash backend is configured raises nothing: vectors load,
queries encode, cosine similarity computes, and every result is noise.

The Docker image bakes the index at build time while the backend is chosen by
an environment variable at run time, so any deployment that changes
EMBEDDING_BACKEND without rebuilding reaches exactly this state.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rag.embeddings import HashEmbedder
from rag.index.store import CorpusIndex, IndexEmbedderMismatch


def _write_meta(directory: Path, embedder_name: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "meta.json").open("w", encoding="utf-8") as handle:
        json.dump({"embedder": embedder_name, "dimensions": 384}, handle)


class TestEmbedderMismatch:
    def test_a_differently_built_index_is_refused(self, tmp_path):
        """bge-built index, hash backend configured: same dimension, no error otherwise."""
        _write_meta(tmp_path, "bge-small")

        with pytest.raises(IndexEmbedderMismatch) as excinfo:
            CorpusIndex.load(tmp_path, HashEmbedder())

        message = str(excinfo.value)
        assert "bge-small" in message
        assert "hash" in message
        assert "EMBEDDING_BACKEND" in message, "the error must say how to fix it"

    def test_a_matching_embedder_passes_the_guard(self, tmp_path):
        """Must fail later on the missing data files, not at the guard."""
        _write_meta(tmp_path, HashEmbedder().name)

        with pytest.raises(Exception) as excinfo:
            CorpusIndex.load(tmp_path, HashEmbedder())

        assert not isinstance(excinfo.value, IndexEmbedderMismatch), (
            "the guard fired on a matching embedder"
        )

    def test_an_index_without_meta_is_not_blocked(self, tmp_path):
        """Older indexes predate meta.json; absence must not be treated as mismatch."""
        tmp_path.mkdir(parents=True, exist_ok=True)

        with pytest.raises(Exception) as excinfo:
            CorpusIndex.load(tmp_path, HashEmbedder())

        assert not isinstance(excinfo.value, IndexEmbedderMismatch)
