# Committed traces

Readable end-to-end runs, kept so a claim in the report can be checked against
a file rather than taken on trust.

**Every line below was verified against the files on disk.** An earlier version
of this README described a live run that had been overwritten, and asserted
content the committed log does not contain. That is recorded in the failure
taxonomy as the same class of mistake the project is about: an unverified claim
that looked checkable because it sat next to a file.

---

## `demo_offline_full_path.log` — deterministic provider

The only trace that covers the **complete** path. Verbatim:

```
analyst       0 requirements extracted
fit_matcher   fit 0.63, coverage 0%
risk_assessor risk 0.09
decide        DECISION: bid ($52.47/Connect)
pricing       bid $68 (band $41-$89)
writer        draft 19 words, revision 0
verify_claims draft 19 words, revision 0; claims: 0 unsupported -> 0
critic        critic sent back
writer        draft 19 words, revision 1
critic        critic sent back
writer        draft 0 words, revision 2
critic        critic sent back
compliance    compliance clear
>>> STOPPED FOR HUMAN APPROVAL - nothing has been sent <<<
cost       : 8 Connects (irreversible)
idempotency: 2abe3820c1d4ed5b40a2088d...
budget     : {'tokens_used': 22481, 'cost_usd': 0.00253, 'steps': 15}
APPROVING -> submitted prop_000001, 8 Connects
connects before 120 / after 112 / spent 8
replayed the same approval a second time -> no further spend. Correct.
```

**What this evidences** — the structural guarantees, end to end:

| Property | Evidence in the file |
| --- | --- |
| Bounded critique loop | Critic sends back exactly twice, the configured `max_revisions`, then proceeds |
| Approval gate | Execution stops; nothing is sent before a human answers |
| Dry-run preview | The exact Connects cost and idempotency key are shown before approval |
| Exactly-once submission | 120 → 112 Connects; a replayed approval spends nothing further |
| Step budget | 15 steps against the 40 allowed |

**What this does NOT evidence.** The proposal *text* is placeholder: the
offline provider writes no real prose, which is why the draft is 19 words, the
Analyst extracts 0 requirements, and claim counts are `0 unsupported -> 0`.
`claims: 0 -> 0` here means **there were no claims to check**, not that a real
draft passed verification. The `tokens_used` and `cost_usd` figures are the
stub's accounting, not a model's. Read this file for control flow only.

## `demo_live_run.log` — live models (Groq)

Reaches `risk_assessor risk 0.10` and stops. It contains **14 HTTP 429
responses** and no Writer, verifier, Critic or approval line. The Groq free
tier's per-minute token ceiling is below what the three-way analysis fan-out
plus a draft requires in a single process.

**What this evidences**: the rate-limit handling and the degraded path under a
real provider. Nothing about draft quality.

## What is not evidenced by any committed trace

**There is no committed trace of a real-model draft passing through claim
verification.** A run earlier in development did produce one — a 184-word draft
reduced to 152 with one unsupported claim removed — but that log was
overwritten by a later throttled run before it was committed, so the claim is
no longer checkable and is not made anywhere in the report.

Closing this needs one command inside quota, not a rebuild:

```bash
EMBEDDING_BACKEND=local python -m scripts.demo_one_job > evals/results/traces/demo_live_full_path.log 2>&1
```

Run it one job at a time, with nothing else hitting the provider.

## Regenerating

```bash
# complete path, deterministic, ~60s, no API key required
EMBEDDING_BACKEND=hash OPEN_LLM_API_KEY="" python -m scripts.demo_one_job

# live models, needs quota headroom
EMBEDDING_BACKEND=local python -m scripts.demo_one_job
```

GigSim must be running (`make gigsim`).
