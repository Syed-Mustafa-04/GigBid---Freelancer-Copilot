# ADR 0002 — Verify claims before the Critic sees the draft

**Status:** accepted · **Date:** 2026-11-06

## Context

Two quality checks run on a draft: claim verification (is every experience
claim supported by retrieved evidence?) and the Critic (is this proposal good
enough to get a reply?). Their order is not arbitrary.

## Decision

Verification runs first. Unsupported claims are removed and partly-supported
claims are rewritten to the weaker true version **before** the Critic reads
anything.

## Why

A model asked to write persuasively produces its most compelling sentences
exactly where it overstates — "extensive experience delivering this at scale"
reads better than "I did this once, in 2023, for a logistics client".

If the Critic ran first it would score the fabricated draft highly, and the
verifier's deletions would then look like a quality regression, inviting a
future maintainer to "fix" the ordering and ship lies.

Running verification first means the Critic only ever judges true sentences,
so its score means what it says.

## Consequences

Drafts reaching the Critic are sometimes noticeably thinner, and the Critic
then reports `too_short`. That is the correct signal: it means the portfolio
does not support the proposal, which is a *retrieval or fit* problem, not a
writing one. The thinness is visible rather than papered over.

Measured: unsupported claims before vs after verification is reported on every
proposal and gated in CI at zero.
