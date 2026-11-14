# Architecture

GigBid finds freelance work worth bidding on, drafts proposals grounded in real
portfolio evidence, and never sends anything without a human approving it.

This document explains the structure and, more usefully, **why each piece
exists** and what breaks without it.

---

## The shape of the system

```
                         ┌──────────────────────────────┐
                         │  Next.js web app (:3000)     │
                         │  inbox · studio · pipeline   │
                         └──────────────┬───────────────┘
                                        │ SSE (node-by-node progress)
                         ┌──────────────▼───────────────┐
                         │  FastAPI (:8000)             │
                         │  auth · routers · service    │
                         └───┬──────────────────────┬───┘
                             │                      │
              ┌──────────────▼────────┐   ┌─────────▼──────────┐
              │  LangGraph            │   │  Worker            │
              │  bid graph            │   │  scouting, long    │
              │  conversation graph   │   │  runs, schedules   │
              └──────────┬────────────┘   └─────────┬──────────┘
                         │                          │
                         │   agent/mcp_client.py (per-agent allowlists)
                         │                          │
        ┌────────────────┼──────────────────────────┼────────────────┐
        │                │                          │                │
 ┌──────▼──────┐  ┌──────▼───────┐  ┌───────────────▼──┐  ┌──────────▼────────┐
 │ Portfolio   │  │ Marketplace  │  │ Workspace        │  │ GitHub MCP        │
 │ MCP (read)  │  │ MCP (r/w)    │  │ MCP (calendar)   │  │ (3rd party, read) │
 └──────┬──────┘  └──────┬───────┘  └──────────────────┘  └───────────────────┘
        │                │
        │     agent/marketplace_port.py  ← ONE interface, two adapters
        │                │
        │       ┌────────┴─────────┐
        │       │                  │
 ┌──────▼─────┐ │  ┌───────────────▼────────┐   ┌────────────────────────┐
 │ RAG index  │ │  │ GigSim (:8081)         │   │ Upwork official MCP    │
 │ dense·bm25 │ │  │ simulator, hidden      │   │ OAuth 2.1, drafts only │
 │ skillgraph │ │  │ client preferences     │   └────────────────────────┘
 └────────────┘ │  └────────────────────────┘
                │
        ┌───────▼─────────────────────────────┐
        │ Postgres: app data + LangGraph      │
        │ checkpoints (durable interrupts)    │
        └─────────────────────────────────────┘
```

---

## The bid graph

One durable thread per job.

```
START
  │
  ├── Send() ──→ Job Analyst ────┐
  ├── Send() ──→ Risk Assessor ──┼──→ join ──→ decide ──→ skip / watch ──→ END
  └── Send() ──→ Fit Matcher ────┘                 │
                                                 bid
                                                   │
       Pricing ──→ Writer ──→ Verify claims ──→ Critic
                      ▲                            │
                      └──── revise (max 2) ────────┘
                                                   │
                            Compliance Guard ──→ blocked ──→ END
                                   │
                            Human approval  ◀── interrupt()
                                   │
                   ┌───────────────┼───────────────┐
                 reject          edit            approve
                   │               │               │
                  END      re-run Compliance     Submit ──→ END
```

### Why each agent exists

| Agent | Exists because | Without it |
| --- | --- | --- |
| **Scout** | Saved searches produce the same job repeatedly under new ids. | Reposts get re-analysed; tokens burn on duplicates. |
| **Job Analyst** | Downstream retrieval needs *atomic* requirements, not a blob. | One averaged query; multi-requirement jobs get evidence for one requirement. |
| **Risk Assessor** | Connects are finite and scams are common. | The user pays to apply to fraud. |
| **Fit Matcher** | Someone must say honestly what the portfolio cannot prove. | The Writer invents the gap. |
| **Pricing Strategist** | Job budgets are aspirational; awarded prices are facts. | Bids anchor on the client's wish, not the market. |
| **Proposal Writer** | Drafting is the actual product. | — |
| **Claim verifier** | A model asked to persuade will overstate. | R5 fails; the system lies to clients. |
| **Critic** | Grounded and good are different properties. | True but generic proposals that get ignored. |
| **Compliance Guard** | Platform rules are not negotiable and not stylistic. | Account suspension. |
| **Client Liaison** | Replies are where work is won or lost, and inbound text is untrusted. | The riskiest surface is unmanaged. |

### Three design decisions worth defending

**`Send()` fan-out, not three sequential edges.** The three analyses read
nothing from each other, so running them concurrently makes the critical path
the slowest of the three rather than their sum. Each writes to its *own* state
key, which is what lets LangGraph merge them without a reducer conflict.

**Claim verification runs *before* the Critic.** The Critic judges
persuasiveness, and a fabricated sentence is frequently the most persuasive one
in a draft. Running the Critic first would have it defend exactly the sentences
that must be deleted, and the deletion would then look like a quality
regression. Verify, strip, then critique what is left.

**An edit re-enters Compliance.** A human editing a draft can introduce a
violation — most obviously their own email address. Trusting the earlier
verdict after the text changed would make the guard decorative.

---

## State, trace, memory — three things, kept apart

Conflating these is what makes agent systems unexplainable.

| | What it holds | Who reads it | Lifetime |
| --- | --- | --- | --- |
| **State** (`agent/state.py`) | What this job needs *now*: JobSpec, reports, draft, decision. | The next node. | One thread. |
| **Execution trace** | Every tool call, argument, result, timing. Append-only. | Humans, the trace viewer, audit. | Stored per run. |
| **Long-term memory** (`agent/memory/`) | *Promoted* knowledge only, via explicit rules. | The Writer and Client Liaison. | Across threads, per user. |

Nothing reaches memory automatically. A style rule requires the same edit
pattern to recur across **distinct** proposals; an outcome rule requires enough
**observed terminal** events. Raw tool output, client PII, job-post text and
model reasoning are on a `never_promote` list. The user can see and delete
everything from Settings — a system that learns from you and will not show you
what it learned is one you cannot correct.

---

## The marketplace port (R1)

Everything the graph knows about "a marketplace" is one Protocol
(`agent/marketplace_port.py`). Two adapters implement it:

| | GigSim | Live Upwork |
| --- | --- | --- |
| Transport | REST | Official MCP server, Streamable HTTP, OAuth 2.1 |
| Writes | Draft + submit | **Draft only** — the user confirms on Upwork |
| Reads | On demand, rate-limited | Scheduled and cached; never polled like a scraper |
| Market stats | From 1,500 historical jobs | **Unavailable** — and we say so rather than scraping |

Switching is one config line. The graph cannot tell which is behind it.

The port — not the adapters — owns the behaviour that must hold for *both*,
because it is a property of talking to someone else's platform: a politeness
rate limiter, bounded retry with jittered backoff, a declared degraded mode
(`MarketplaceUnavailable`, never an empty list), and an append-only audit log.

---

## Safety, in layers

| Layer | Mechanism | Generalises? |
| --- | --- | --- |
| 1. Structural separation | Untrusted text is delimited and framed as data. | Yes |
| 2. Pattern detection | Known injection shapes flagged and reported. | No — evadable by rephrasing |
| 3. Neutralisation | Detected spans have their imperative force broken, but stay visible. | Partly |
| 4. **Capability denial** | No analysis agent holds a write tool. The one that does refuses without a human approval token. | **Yes — structural** |

Layer 4 is the one that matters. Layers 1–3 reduce how often an attack lands;
layer 4 makes a landed attack survivable. An injection that defeats every
earlier layer still cannot submit a proposal, because the capability is not
reachable from the model. That is asserted by test, not measured by sampling.

The Job Analyst — the node that reads attacker-controlled text — holds **no
tools at all**. That combination (reads hostile input, can act) is the whole
danger, and it is designed out rather than prompted away.

---

## Durability (C6)

The LangGraph checkpointer is Postgres in production, SQLite locally. When the
graph hits `interrupt()`, the thread is a **row in a database**, not an object
in memory. So:

* the API can restart while an approval is pending and lose nothing;
* a browser refresh re-reads the pending approval from the server;
* resuming with `Command(resume=...)` continues from that exact node;
* a replayed approval cannot double-submit, because the idempotency key is
  derived from the thread and the approved text and lives in the same state.

`tests/integration/test_graph_guarantees.py::test_approval_survives_a_restart_and_submits_exactly_once`
proves this by closing the database connection mid-approval and resuming from a
fresh process.

---

## Configuration is the change surface

`agent/config.yaml` holds every rule, threshold, budget, model route and
bidding economic. `.env` holds secrets and endpoints only. Nothing behavioural
is hard-coded in Python, which is what makes the platform-policy change request
a configuration edit rather than a refactor. See
[`adr/0006-change-request.md`](adr/0006-change-request.md).

Any value can be overridden per-process with `GIGBID__SECTION__KEY`, which is
how the ablation runner sweeps settings without editing the file.

---

## Where it is weakest

Stated here rather than discovered at viva:

1. **The corpus is synthetic.** Gold evidence labels are knowable because jobs
   were generated from known projects. That makes the retrieval metrics exact
   and the realism arguable. See [`DATA_CARD.md`](DATA_CARD.md).
2. **Cross-encoder reranking cannot ship on a CPU host.** Measured at 53s/job
   over 40 candidates — twice the entire per-job latency budget. Capped to 10
   candidates and reported honestly in [`adr/0004-rag-variant.md`](adr/0004-rag-variant.md).
3. **Pattern-based injection detection does not generalise.** A novel phrasing
   that reads as a legitimate client request passes layers 2 and 3. Layer 4 is
   why that is survivable rather than fatal.
4. **An injection that persuades the *human* is out of scope.** Every defence
   here protects the agent, not the person reading the approval screen.
