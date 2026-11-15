# Runbook

How to run GigBid, what breaks, and what to do about it.

---

## Quick start

```bash
cp .env.example .env          # fill APP_SECRET_KEY and REVIEWER_PASSWORD
make install                  # venv + Python deps + npm
make seed                     # corpus, jobs, dev set, prompts (seed 20261115)
make models                   # download the open embedding + reranker models
```

Then either:

```bash
make up                       # docker compose: everything
# or, in four terminals:
make gigsim                   # :8081  marketplace simulator
make api                      # :8000  FastAPI
make worker                   #        scouting + long runs
make web                      # :3000  Next.js
```

Sign in at <http://localhost:3000> with `REVIEWER_EMAIL` / `REVIEWER_PASSWORD`.

### Minimum viable check

```bash
curl localhost:8081/health                 # GigSim: jobs_total > 0
curl localhost:8000/health                 # API
curl localhost:8000/system/status | jq     # marketplace reachable? LLM offline?
```

`/system/status` is the first thing to read on any problem. It says which
marketplace is connected, whether it is reachable, and whether the LLM is the
offline stub.

---

## Models

GigBid runs with no API key, on a deterministic offline provider. The graph
executes fully; the text and the scores are placeholders. The UI says so with a
persistent banner, and every results file records `provider: offline_stub`.

For real output, set an OpenAI-compatible endpoint:

```bash
OPEN_LLM_BASE_URL=https://api.groq.com/openai/v1
OPEN_LLM_API_KEY=gsk_...
```

Any OpenAI-compatible provider works: Groq, OpenRouter, Together, Ollama
(`http://localhost:11434/v1`), vLLM. Per-node routing lives in
`agent/config.yaml` under `models.profiles.open.nodes`.

Embeddings and the reranker are open models in **both** profiles.
`EMBEDDING_BACKEND` is `local` (bge-small; the default, and what the reported
numbers use), `hosted`, or `hash` (offline fallback, no download, markedly
weaker, tagged in every results file).

---

## Scheduling and rate limits

Saved searches run in the worker, never in a web request. The politeness floor
is `marketplace.rate_limit.min_scout_interval_seconds`, default **1800 s**.

**Why 30 minutes.** Upwork treats aggressive automated polling as abuse. A
freelancer checking a saved search every half hour is ordinary human
behaviour; every 30 seconds is not, and that difference is what separates a
tool from a bot. It also costs nothing real: most postings collect proposals
for days, so a 30-minute lag does not lose work. The token bucket (20 req/min,
burst 5) sits far below GigSim's 60/min, so the adapter is never the reason a
limit is hit.

Live mode additionally **caches reads** per tool for the same interval: a
caller asking sooner gets the cached response rather than a new platform
request.

---

## Things that break

### "The marketplace is not responding"

A red banner, and job lists may be stale. This is an outage signal, not an
empty result; the two are deliberately never shown the same way.

```bash
curl localhost:8081/health
docker compose logs gigsim --tail=50
```

Queued work resumes automatically. Nothing is lost: pending approvals live in
the checkpointer, not in memory.

### An approval is stuck

```bash
curl -H "Authorization: Bearer $TOKEN" localhost:8000/approvals
```

If the API restarted mid-approval the thread is still suspended in the
checkpointer and the studio page re-reads it. If `/approvals` is empty but the
pipeline shows one pending, the thread finished: check `/runs/<thread_id>` for
the terminal reason.

### A thread is in `needs_attention`

Deliberate: a budget was exceeded, a node failed repeatedly, or compliance
blocked the draft. `terminal_reason` says which. These are never retried
automatically, because silently retrying a poisoned job is how a worker burns
a budget overnight.

### A thread is in `dead_letter`

Failed `scheduling.dead_letter_after_attempts` times (default 3). It will not
be retried. Inspect the trace, fix the cause, re-queue manually.

### Evaluations are slow

The cross-encoder is why. Measured at **~1.3 s per query-document pair on CPU**,
so reranking 40 candidates costs ~53 s per job.
`rag.variants.v3_rerank.rerank_candidates` caps how many reach it (default 10).
For a fast pass:

```bash
GIGBID__MODELS__RERANKER__BACKEND=none make eval-rag
```

### "unable to open database file" (Windows)

sqlite cannot open a Git Bash `/c/...` path. `run_api.sh` resolves paths with
`cygpath -m` for this reason. Setting `DATABASE_URL` by hand needs a native
path: `sqlite:///C:/path/to/gigbid.sqlite`.

---

## Budgets

Declared in `agent/config.yaml: budgets`, enforced in `agent/nodes/common.py`.

| Budget | Default | On breach |
| --- | --- | --- |
| Tokens per job | 45,000 | thread to `needs_attention` |
| Graph steps | 40 | thread to `needs_attention` |
| Critic revisions | 2 | draft proceeds, issues listed for the human |
| USD per job | 0.02 | thread to `needs_attention` |
| p95 latency per job | 25 s target | reported, not enforced |

Measured values live in `evals/results/decision_quality.json` under
`cost_and_latency`, and on every run at `/runs/<id>`.

The token cap is **thread-wide**. An earlier version charged drafting against a
separate per-proposal budget while the analysis nodes had already spent from
the same counter; the Writer was starved on long jobs and the thread died with
a usable analysis and no draft. The per-proposal figure is now an accounting
line, not a second hard limit.

---

## Switching to live Upwork

1. Generate a token encryption key. Without it the store **refuses** to save
   rather than writing a token in plaintext:
   ```bash
   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
   ```
2. Set `TOKEN_ENCRYPTION_KEY`, `UPWORK_CLIENT_ID`, `UPWORK_CLIENT_SECRET`.
3. Connect the account in Settings (OAuth 2.1).
4. Set `MARKETPLACE_MODE=upwork_live`.

**What changes:** the adapter behind the port, and nothing else. No node, no
prompt, no graph edge.

**What does not change:** the approval gate, the claim verifier, the compliance
guard, the audit log.

**What live mode will not do:** submit. Upwork requires the user to confirm
on-platform, so `submit_proposal` raises rather than submitting and GigBid
creates the draft for the user to confirm. Live mode is never graded on
outcomes.

---

## Observability

`observability.backend` selects Langfuse, LangSmith or none. One trace flows
from the UI request through the API, the graph, every MCP server and the audit
log; each node records tokens, latency and cost. PII is masked before anything
is written.

Without a tracing backend, `/runs/<thread_id>` still shows the full
node-by-node trace from the database.

---

## Backup

Two stores, both needed:

```bash
pg_dump gigbid > backup.sql     # app data AND LangGraph checkpoints
```

Losing the checkpointer loses every pending approval. Losing the app database
loses the pipeline and analytics but not in-flight threads.

## Running without `make` (Windows)

`make` is not installed on the Windows development machine this was built on.
Every `make <target>` in these docs is still correct on Linux, macOS and inside
the Docker images; this table is the direct equivalent for a shell that has no
`make`. `$(PY)` is `.venv/Scripts/python.exe` on Windows and `.venv/bin/python`
elsewhere.

| Target | Direct equivalent |
| --- | --- |
| `make install` | `python -m venv .venv` then `.venv/Scripts/python.exe -m pip install -e ".[dev,embed]"` |
| `make seed` | `.venv/Scripts/python.exe -m scripts.generate_corpus --seed 20261115` then the same for `generate_jobs` |
| `make gigsim` | `.venv/Scripts/python.exe -m uvicorn gigsim.server:app --port 8081 --reload` |
| `make api` | `.venv/Scripts/python.exe -m uvicorn apps.api.main:app --port 8000 --reload` |
| `make worker` | `.venv/Scripts/python.exe -m worker.tasks` |
| `make web` | `cd apps/web && npm run dev` |
| `make test` | `.venv/Scripts/python.exe -m pytest -q` |
| `make lint` | `.venv/Scripts/python.exe -m ruff check .` |
| `make snapshot` | `.venv/Scripts/python.exe -m tests.contract.record_snapshot` |
| `make up` / `make down` | `cd infra && docker compose --env-file ../.env up --build -d` / `... down -v` |

### Evaluations, one at a time

The free LLM tier allows 25 requests and 12,000 tokens per minute. Running two
of these at once is what exhausted the quota during evaluation and left five
result files unproduced, so run them **singly**, with nothing else hitting the
provider.

| Evaluation | Command | Needs a model? |
| --- | --- | --- |
| RAG variants | `EMBEDDING_BACKEND=local $(PY) -m evals.runners.rag_eval --limit 90 --k 5` | no |
| Ablation | `EMBEDDING_BACKEND=local $(PY) -m evals.runners.ablation --limit 60` | no |
| Tool granularity | `$(PY) -m evals.runners.tool_granularity` | no |
| Injection defence | `$(PY) -m evals.runners.injection_eval` | no |
| Decision quality | `$(PY) -m evals.runners.decision_eval --limit 30 --concurrency 1` | **yes** |
| Multi-agent comparison | `$(PY) -m evals.runners.multi_agent_comparison --limit 30` | **yes** |
| GigSim outcomes | `$(PY) -m evals.runners.gigsim_outcomes --limit 40` | **yes** |
| Proposal experiment | `$(PY) -m evals.runners.proposal_experiment --limit 40` | **yes** |
| Judge calibration | `$(PY) -m evals.runners.judge_calibration` | **yes**, plus 30 hand labels |

`--concurrency 1` on the decision eval is not a tuning preference: the runner
defaults to 4, which on a free tier produces sustained 429s and no result file.
It is the only runner that fans out; the rest are already sequential.

**Check `n_completed`, not just `provider`, before citing any result file.**

All five model-dependent runners write `"provider": "live_model"` or
`"provider": "offline_stub"`, and an `offline_stub` run produces a complete,
plausible-looking file whose numbers mean nothing. So that field is worth
checking -- but it is **not sufficient**, and trusting it alone was a mistake
already made here.

`provider` derives from `LLMClient.is_offline`, which tests only whether a base
URL and API key are *configured*. It is a configuration check, not a liveness
check. A run where the key was valid and 98% of completions were rejected with
HTTP 429 still reports `live_model`.

That exact run happened: 30 jobs, `n_completed: 0`, `n_errored: 30`, 741
rate-limit responses against 14 successes, and every headline metric `0.0`
because the metric functions were handed empty lists. The file reads as a
system that scored zero on everything. It scored nothing at all.

So the check is:

| Field | What it tells you |
| --- | --- |
| `provider` | whether a key was configured -- necessary, not sufficient |
| **`n_completed`** | **how many jobs actually produced a decision** |
| `n_errored` | how many died |

If `n_completed` is 0, every other number in the file is an artefact of
averaging an empty list, including the Wilson intervals, which will print
`[0.0, 0.0]` as though that were a measurement.
