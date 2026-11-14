# ADR 0001 — One marketplace interface, two adapters

**Status:** accepted · **Date:** 2026-11-05

## Context

GigBid must work against GigSim for development and grading, and against
Upwork's official MCP server in live mode. The two differ in transport (REST
vs Streamable HTTP + OAuth 2.1), in capability (GigSim can submit; Upwork only
accepts drafts the user confirms), and in what they expose (GigSim serves
market statistics; Upwork has no aggregate-price tool at all).

The tempting shortcut is a mode flag threaded through the nodes.

## Decision

One `Protocol` — `agent/marketplace_port.py` — is the only thing the graph
knows about a marketplace. Two adapters implement it. `marketplace.mode`
selects one. No node imports an adapter.

The **port**, not the adapters, owns rate limiting, bounded retry with
jittered backoff, degraded-mode signalling and the audit log, because those
are properties of calling someone else's platform rather than of either
platform.

## Consequences

Good: switching marketplaces is one config line; the graph is untestable
against the wrong one by construction; adding a third marketplace is one file.

Bad: capability differences must be handled *inside* the adapter. Live
`get_market_stats` cannot return real data, so it returns
`source: "unavailable_in_live_mode"` with a note — deliberately visible rather
than silently returning zeros, because a silent zero would make the Pricing
Strategist bid at the floor.

Live `submit_proposal` **raises** rather than submitting: Upwork requires the
user to confirm on-platform. That asymmetry is honest and is stated in the UI.
