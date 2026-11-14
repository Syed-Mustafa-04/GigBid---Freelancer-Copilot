# ADR 0005 — A deterministic offline LLM provider

**Status:** accepted · **Date:** 2026-11-09

## Context

Every node calls a model. Without an API key the obvious outcomes are: the
system does not run at all, or tests mock each node and stop exercising the
graph.

Both are bad. The first makes the project undemonstrable on a dead network;
the second means CI passes while the real wiring is broken.

## Decision

`agent/llm.py: OfflineProvider` returns deterministic, **schema-valid** output
derived from a hash of the prompt. It resolves `$ref`/`$defs`, `anyOf` and
string formats, so it satisfies nested Pydantic hand-offs such as
`ProposalDraft`.

It engages automatically when no API key is configured.

## Why

The whole graph executes: fan-out, reducers, routing, the critique loop, the
interrupt, checkpointing, idempotent submission. Those are the parts most
likely to break and they are exercised for real, free, in CI, reproducibly.

A test failure is then a code change rather than a model's mood.

## The risk, and how it is handled

The danger is mistaking offline output for a measurement. So:

* every results file records `provider: offline_stub`;
* runners that depend on model judgement print a warning naming what is and
  is not a measurement;
* the UI shows a persistent banner (`banner-offline-llm`) stating that the
  text and scores are placeholders;
* the report presents no model-quality number produced this way.

## Consequences

Offline fit and risk scores are meaningless noise, so decision accuracy under
the stub measures the *rule* path only. That is stated wherever it appears.
