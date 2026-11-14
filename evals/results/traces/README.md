# Committed traces

Readable end-to-end runs, kept so a claim in the report can be checked against
a file rather than taken on trust.

| File | Provider | What it shows |
| --- | --- | --- |
| `demo_offline_full_path.log` | **deterministic offline** | The complete path: analysis, bid decision, pricing, drafting, claim verification, two bounded Critic revisions, compliance, the human approval gate, submission, and the exactly-once replay check. |
| `demo_live_run.log` | **live model (Groq)** | A run against real models, truncated by free-tier rate limiting. Shows the real-provider failure path. |

## Why the full path trace uses the offline provider

Not because the system cannot do it with a real model - it can, and the live
log shows the same nodes producing real analyses (fit 0.95, six extracted
requirements, a bid priced inside the market band). It is because the Groq free
tier's per-minute token ceiling is below what the three-way analysis fan-out
plus a draft needs, so a complete single-process run hits 429s partway through.

The trade is deliberate: a **complete** trace of the structural guarantees
beats a **partial** trace with better prose in it. What matters in this file is
the control flow, and that is identical under either provider:

```
decide     DECISION: bid ($52.47/Connect)
pricing    bid $68 (band $41-$89)
writer     draft, revision 0
critic     sent back              <- bounded loop, revision 1
critic     sent back              <- revision 2, the configured maximum
compliance clear
>>> STOPPED FOR HUMAN APPROVAL - nothing has been sent <<<
cost       : 8 Connects (irreversible)
idempotency: 2abe3820c1d4ed5b40a2088d...
APPROVING -> submitted prop_000001, 8 Connects
connects before 120 / after 112 / spent 8
replayed the same approval -> no further spend. Correct.
```

The proposal **text** in this file is placeholder - the offline provider writes
no real prose, which is why the draft is 19 words and the claim counts are
zero. Read it for the control flow, not the writing. For real drafting quality,
see the live log and `evals/results/rag_variants.json`.

To regenerate either:

```bash
# full path, deterministic, ~60s, no API key needed
EMBEDDING_BACKEND=hash OPEN_LLM_API_KEY="" python -m scripts.demo_one_job

# live models, needs quota
EMBEDDING_BACKEND=local python -m scripts.demo_one_job
```
