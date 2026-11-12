"""GigSim - the marketplace simulator GigBid develops and is graded against.

This stands in for a real platform. It is deliberately hostile in the ways a
real one is:

* a token-bucket **rate limit** that returns 429 with ``Retry-After``;
* **fault injection** - a configurable fraction of requests fail with 500/503
  so the adapter's retry/backoff and degraded mode are exercised for real;
* **idempotency keys** on every write, so a retried submit never double-spends
  Connects;
* hidden client preferences that decide reply/interview/hire.

Run:  uvicorn gigsim.server:app --port 8081
Env:  GIGSIM_API_TOKEN, GIGSIM_FAULT_RATE, GIGSIM_RATE_LIMIT_RPM, GIGSIM_SEED
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import time
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import asdict
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from gigsim.world import World

API_TOKEN = os.getenv("GIGSIM_API_TOKEN", "gigsim-dev-token")
FAULT_RATE = float(os.getenv("GIGSIM_FAULT_RATE", "0.03"))
RATE_LIMIT_RPM = int(os.getenv("GIGSIM_RATE_LIMIT_RPM", "60"))
SEED = int(os.getenv("GIGSIM_SEED", "20261115"))

world = World(seed=SEED)
_fault_rng = random.Random(SEED + 99)
_buckets: dict[str, deque[float]] = {}


# ---------------------------------------------------------------------------
# Auth, rate limiting, faults
# ---------------------------------------------------------------------------
async def require_token(authorization: str = Header(default="")) -> str:
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    token = authorization.removeprefix("Bearer ").strip()
    if token != API_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid token")
    return token


def _rate_limit(key: str) -> None:
    now = time.monotonic()
    bucket = _buckets.setdefault(key, deque())
    while bucket and now - bucket[0] > 60.0:
        bucket.popleft()
    if len(bucket) >= RATE_LIMIT_RPM:
        retry_after = max(1, int(60 - (now - bucket[0])))
        raise HTTPException(
            status_code=429,
            detail=f"Rate limit exceeded: {RATE_LIMIT_RPM} requests/minute",
            headers={"Retry-After": str(retry_after)},
        )
    bucket.append(now)


@asynccontextmanager
async def lifespan(_: FastAPI):
    print(
        f"[gigsim] world loaded: {len(world.jobs)} jobs, {len(world.clients)} clients, "
        f"{len(world.historical)} historical | rate_limit={RATE_LIMIT_RPM}/min fault_rate={FAULT_RATE}"
    )
    yield


app = FastAPI(
    title="GigSim",
    description="Freelance marketplace simulator for the GigBid assignment",
    version="1.0.0",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)


@app.middleware("http")
async def guardrails(request: Request, call_next):
    path = request.url.path
    if path in {"/health", "/docs", "/openapi.json", "/redoc"}:
        return await call_next(request)

    client_key = request.headers.get("authorization", "anon")[-16:]
    try:
        _rate_limit(client_key)
    except HTTPException as exc:
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": exc.detail, "code": "rate_limited"},
            headers=exc.headers or {},
        )

    # Fault injection on reads only: a write failing mid-flight is covered by
    # the idempotency tests instead, which is the harder property to get right.
    if request.method == "GET" and _fault_rng.random() < FAULT_RATE:
        status = _fault_rng.choice([500, 502, 503])
        return JSONResponse(
            status_code=status,
            content={"error": "Simulated upstream failure", "code": "injected_fault"},
        )

    return await call_next(request)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------
class ProposalDraftIn(BaseModel):
    job_id: str
    freelancer_id: str = "p_ai_engineer"
    cover_letter: str
    screening_answers: list[dict[str, Any]] = Field(default_factory=list)
    amount_usd: float = Field(ge=0)
    dry_run: bool = False


class SubmitIn(BaseModel):
    proposal_id: str
    dry_run: bool = False


class MessageIn(BaseModel):
    thread_id: str
    job_id: str = ""
    body: str
    freelancer_id: str = "p_ai_engineer"
    dry_run: bool = False


class AdvanceIn(BaseModel):
    hours: int = Field(default=24, ge=1, le=720)


# ---------------------------------------------------------------------------
# Read endpoints
# ---------------------------------------------------------------------------
@app.get("/health")
async def health() -> dict[str, Any]:
    return {"status": "ok", "service": "gigsim", "version": "1.0.0", **world.stats()}


@app.get("/jobs", dependencies=[Depends(require_token)])
async def search_jobs(
    query: str = "",
    skills: str = "",
    budget_min: float | None = None,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    posted_after: str | None = None,
) -> dict[str, Any]:
    skill_list = [s.strip() for s in skills.split(",") if s.strip()]
    jobs, total = world.search_jobs(
        query, skills=skill_list, budget_min=budget_min,
        limit=limit, offset=offset, posted_after=posted_after,
    )
    return {"jobs": jobs, "total": total, "limit": limit, "offset": offset}


@app.get("/jobs/{job_id}", dependencies=[Depends(require_token)])
async def get_job(job_id: str) -> dict[str, Any]:
    job = world.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job not found: {job_id}")
    return job


@app.get("/clients/{client_id}/history", dependencies=[Depends(require_token)])
async def client_history(client_id: str) -> dict[str, Any]:
    history = world.client_history(client_id)
    if history is None:
        raise HTTPException(status_code=404, detail=f"Client not found: {client_id}")
    return history


@app.get("/market/stats", dependencies=[Depends(require_token)])
async def market_stats(skill: str = Query(...)) -> dict[str, Any]:
    return world.market_stats(skill)


@app.get("/connects/balance", dependencies=[Depends(require_token)])
async def connects_balance(freelancer_id: str = "p_ai_engineer") -> dict[str, Any]:
    return world.connects_balance(freelancer_id)


@app.get("/proposals", dependencies=[Depends(require_token)])
async def list_proposals(freelancer_id: str | None = None) -> dict[str, Any]:
    items = [
        asdict(p) for p in world.proposals.values()
        if freelancer_id is None or p.freelancer_id == freelancer_id
    ]
    return {"proposals": items, "total": len(items)}


@app.get("/messages", dependencies=[Depends(require_token)])
async def list_messages(thread_id: str | None = None) -> dict[str, Any]:
    items = [asdict(m) for m in world.list_messages(thread_id)]
    return {"messages": items, "total": len(items)}


# ---------------------------------------------------------------------------
# Write endpoints - every one takes an idempotency key
# ---------------------------------------------------------------------------
@app.post("/proposals", dependencies=[Depends(require_token)], status_code=201)
async def create_proposal(
    payload: ProposalDraftIn,
    idempotency_key: str = Header(default="", alias="Idempotency-Key"),
) -> dict[str, Any]:
    if not idempotency_key:
        raise HTTPException(status_code=400, detail="Idempotency-Key header is required on writes")
    job = world.get_job(payload.job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job not found: {payload.job_id}")

    if payload.dry_run:
        return {
            "dry_run": True,
            "would_create": {
                "job_id": payload.job_id,
                "amount_usd": payload.amount_usd,
                "cover_letter_preview": payload.cover_letter[:400],
                "cover_letter_words": len(payload.cover_letter.split()),
                "screening_answers": len(payload.screening_answers),
            },
            "connects_cost": 0,
            "note": "Creating a draft costs no Connects. Submitting it does.",
        }

    proposal, replayed = world.create_proposal_draft(
        job_id=payload.job_id,
        freelancer_id=payload.freelancer_id,
        cover_letter=payload.cover_letter,
        screening_answers=payload.screening_answers,
        amount_usd=payload.amount_usd,
        idempotency_key=idempotency_key,
    )
    return {"proposal": asdict(proposal), "idempotent_replay": replayed}


@app.post("/proposals/submit", dependencies=[Depends(require_token)])
async def submit_proposal(
    payload: SubmitIn,
    idempotency_key: str = Header(default="", alias="Idempotency-Key"),
) -> dict[str, Any]:
    if not idempotency_key:
        raise HTTPException(status_code=400, detail="Idempotency-Key header is required on writes")
    proposal = world.proposals.get(payload.proposal_id)
    if proposal is None:
        raise HTTPException(status_code=404, detail=f"Proposal not found: {payload.proposal_id}")
    job = world.jobs.get(proposal.job_id, {})
    cost = int(job.get("connects_cost", 8))

    if payload.dry_run:
        balance = world.connects_balance(proposal.freelancer_id)
        return {
            "dry_run": True,
            "would_submit": payload.proposal_id,
            "connects_cost": cost,
            "connects_balance_before": balance["balance"],
            "connects_balance_after": balance["balance"] - cost,
            "irreversible": True,
        }

    try:
        submitted, replayed = world.submit_proposal(payload.proposal_id, idempotency_key)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {
        "proposal": asdict(submitted),
        "idempotent_replay": replayed,
        "connects_spent": submitted.connects_spent,
    }


@app.post("/messages", dependencies=[Depends(require_token)], status_code=201)
async def send_message(
    payload: MessageIn,
    idempotency_key: str = Header(default="", alias="Idempotency-Key"),
) -> dict[str, Any]:
    if not idempotency_key:
        raise HTTPException(status_code=400, detail="Idempotency-Key header is required on writes")
    if payload.dry_run:
        return {
            "dry_run": True,
            "would_send": {"thread_id": payload.thread_id, "body_preview": payload.body[:400]},
            "connects_cost": 0,
        }
    message, replayed = world.send_message(
        thread_id=payload.thread_id,
        job_id=payload.job_id,
        body=payload.body,
        freelancer_id=payload.freelancer_id,
        idempotency_key=idempotency_key,
    )
    return {"message": asdict(message), "idempotent_replay": replayed}


# ---------------------------------------------------------------------------
# Simulation control (not part of the agent-facing surface)
# ---------------------------------------------------------------------------
@app.post("/admin/advance")
async def advance(payload: AdvanceIn) -> dict[str, Any]:
    return world.advance(payload.hours)


@app.post("/admin/reset")
async def reset() -> dict[str, Any]:
    world.reset()
    return {"status": "reset", **world.stats()}


@app.get("/admin/audit")
async def audit(limit: int = 100) -> dict[str, Any]:
    return {"entries": world.audit[-limit:], "total": len(world.audit)}


@app.get("/admin/stats")
async def stats() -> dict[str, Any]:
    return world.stats()


@app.get("/stream/jobs", dependencies=[Depends(require_token)])
async def stream_jobs(interval: float = 2.0) -> StreamingResponse:
    """SSE stream of newly released jobs, for the Scout's live mode."""

    async def generator():
        seen: set[str] = set(world._released)
        for _ in range(120):
            await asyncio.sleep(interval)
            world.advance(hours=1)
            new = [j for j in world._released if j not in seen]
            seen.update(new)
            for job_id in new:
                job = world.get_job(job_id)
                if job:
                    yield f"data: {json.dumps(job)}\n\n"

    return StreamingResponse(generator(), media_type="text/event-stream")
