# ADR 0003 — A numpy vector store, not a vector database

**Status:** accepted · **Date:** 2026-11-07

## Context

The corpus is ~954 chunks at 384 dimensions: a 1.4 MB float32 matrix. The
default instinct is to add Qdrant or Chroma to `docker compose`.

## Decision

A hand-written store (`rag/index/store.py`): a numpy matrix plus aligned
metadata, cosine similarity by one matmul, persisted as `.npy` + JSON.

## Why

At this size a full scan is sub-millisecond — the ANN index a vector database
provides is an optimisation for a problem we do not have. Against that, a
database adds a service to compose, a failure mode to the health check, a
version to pin, and a schema to migrate.

The interface is the same either way, so the decision is reversible: a
`QdrantStore` behind `VectorStore` is a contained change if the corpus grows
past roughly 10⁵ chunks, where the scan starts to cost more than a network hop.

## Consequences

Good: one fewer service; the index builds and persists in-process; CI needs no
container.

Bad: no ANN, no sharding, no concurrent writers. The index is rebuilt rather
than updated incrementally when the corpus changes — acceptable at 112 s for
the full corpus, and the Dockerfile warms it at build time so a user never
waits for it.
