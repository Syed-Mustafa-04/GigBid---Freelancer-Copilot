.DEFAULT_GOAL := help
SHELL := /bin/bash
PY := .venv/Scripts/python.exe
ifeq (,$(wildcard .venv/Scripts/python.exe))
PY := .venv/bin/python
endif
SEED ?= 20261115

.PHONY: help install seed index api gigsim worker web dev test test-unit test-integration \
        test-contract lint fmt typecheck eval eval-rag eval-decision eval-tools eval-agents \
        eval-injection eval-ablation eval-outcomes demo e2e up down logs clean snapshot

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

# --------------------------------------------------------------- setup ----
install:  ## Create the venv and install Python + web dependencies
	uv venv --python 3.12 .venv || python -m venv .venv
	uv pip install --python $(PY) -e ".[dev,embed]" || $(PY) -m pip install -e ".[dev,embed]"
	cd apps/web && npm install --no-audit --no-fund

seed:  ## Generate the synthetic corpus, jobs and the dev evaluation set
	$(PY) -m scripts.generate_corpus --seed $(SEED)
	$(PY) -m scripts.generate_jobs --seed $(SEED)
	$(PY) scripts/write_prompts.py

index:  ## Build and persist the vector index (slow on first run)
	$(PY) -c "from rag.retriever import get_index; print(get_index().stats())"

models:  ## Download the open embedding and reranker models
	$(PY) scripts/download_models.py

# ----------------------------------------------------------------- run ----
gigsim:  ## Run the marketplace simulator on :8081
	$(PY) -m uvicorn gigsim.server:app --port 8081 --reload

api:  ## Run the API on :8000
	./run_api.sh --reload

worker:  ## Run the background worker (scouting, long runs)
	$(PY) -m worker.tasks

web:  ## Run the Next.js dev server on :3000
	cd apps/web && npm run dev

dev:  ## Everything at once, locally, without Docker
	@echo "Start these in separate terminals: make gigsim | make api | make worker | make web"

# One .env at the repo root is the single source of secrets. Compose would
# otherwise look for infra/.env and silently find nothing.
COMPOSE := docker compose --env-file ../.env -f docker-compose.yml

up:  ## docker compose up (web, api, worker, gigsim, postgres, redis, MCP servers)
	@test -f .env || (echo "No .env found. Run: cp .env.example .env" && exit 1)
	cd infra && $(COMPOSE) up --build -d
	@echo "web http://localhost:3000   api http://localhost:8000/docs   gigsim http://localhost:8081/health"

down:  ## Stop and remove the stack
	cd infra && $(COMPOSE) down -v

logs:  ## Tail the compose logs
	cd infra && $(COMPOSE) logs -f --tail=100

config:  ## Validate the compose file with the real .env
	cd infra && $(COMPOSE) config --quiet && echo "compose config valid"

# --------------------------------------------------------------- tests ----
test:  ## Run every Python test
	$(PY) -m pytest -q

test-unit:
	$(PY) -m pytest tests/unit -q

test-integration:  ## Graph runs against GigSim (GigSim must be up)
	$(PY) -m pytest tests/integration -q

test-contract:  ## MCP tool-schema snapshots; fails when a signature changes
	$(PY) -m pytest tests/contract -q

snapshot:  ## Re-record the MCP tool-schema snapshot after an INTENTIONAL change
	$(PY) -m tests.contract.record_snapshot

e2e:  ## Playwright end-to-end tests (web and api must be up)
	cd apps/web && npx playwright install --with-deps chromium && npm run test:e2e

lint:
	$(PY) -m ruff check .
	cd apps/web && npx tsc --noEmit

fmt:
	$(PY) -m ruff check --fix .
	$(PY) -m ruff format .

typecheck:
	$(PY) -m mypy agent rag apps/api --ignore-missing-imports

# ----------------------------------------------------------- evaluation ----
#
# On a free LLM tier (25 requests/min, 12,000 tokens/min) these must be run
# ONE AT A TIME. Two concurrent callers is what exhausted the quota during
# evaluation and left five result files unproduced. `make eval` runs the
# model-dependent targets back to back and will not finish on a free tier;
# it is kept for a paid key.
#
# The four targets that need no model -- eval-rag, eval-tools, eval-injection,
# eval-ablation -- can be run any time.

eval: eval-rag eval-decision eval-tools eval-agents eval-injection  ## Run every evaluation (needs paid quota)

eval-rag:  ## R&D B: compare all 10 retrieval variants
	EMBEDDING_BACKEND=local $(PY) -m evals.runners.rag_eval --limit 90 --k 5

eval-decision:  ## Bid/skip F1, scam precision and recall, price in band
# --concurrency 1 is not a tuning choice. The runner defaults to 4, which on a
# free tier produces sustained 429s and no result file. Raise it only with a
# paid key. 120 jobs sequentially is slow; override with LIMIT=30 to start.
	$(PY) -m evals.runners.decision_eval --limit $(or $(LIMIT),120) --concurrency $(or $(CONCURRENCY),1)

eval-tools:  ## R&D A: tool granularity
	$(PY) -m evals.runners.tool_granularity

eval-agents:  ## R&D C: single agent vs supervisor vs this graph
	$(PY) -m evals.runners.multi_agent_comparison --limit 30

eval-injection:  ## Injection attack success rate, before and after each layer
	$(PY) -m evals.runners.injection_eval

eval-ablation:  ## Ablate the shipped pipeline one component at a time
	EMBEDDING_BACKEND=local $(PY) -m evals.runners.ablation --limit 60

eval-outcomes:  ## GigSim reply and hire rate on a fixed job set
	$(PY) -m evals.runners.gigsim_outcomes --limit 40

eval-judge:  ## Calibrate the LLM judge against 30 hand labels
	$(PY) -m evals.runners.judge_calibration

# ---------------------------------------------------------------- demo ----
demo:  ## Seed, start GigSim, analyse one job end to end
	$(MAKE) seed
	@echo "Start GigSim (make gigsim) in another terminal, then:"
	@echo "  $(PY) -m scripts.demo_one_job"

clean:
	rm -rf .pytest_cache .ruff_cache .mypy_cache rag/index/store data/processed \
	       gigbid.sqlite gigbid_checkpoints.sqlite apps/web/.next *.log
