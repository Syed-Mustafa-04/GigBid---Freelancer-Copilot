"""Background work (E1): scouting and long agent runs, never in a web request.

Why a worker at all: a scouting pass over several saved searches followed by a
full analysis of each new job takes minutes. Doing that inside an HTTP request
means a timeout, a retry, and a double-analysis. So the API queues and returns,
and this runs the work.

Two properties the restart test exercises:

* **Idempotent processing.** A job is keyed by ``(user_id, job_id)``; a worker
  that dies mid-analysis and restarts resumes the same LangGraph thread rather
  than starting a second one, so a crash cannot produce two proposals for one
  job or spend Connects twice.
* **A dead letter state.** After ``dead_letter_after_attempts`` failures the
  thread stops being retried and becomes visible in the UI as needing
  attention. Infinite retry of a poisoned job is how a worker quietly burns a
  budget all night.

Celery with Redis when available; a built-in asyncio scheduler otherwise, so
``make worker`` runs with no broker during development.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from agent.config import Secrets, get_config
from agent.nodes.scout import Scout
from apps.api import service
from apps.api.db import SavedSearch, Thread, User, get_session_factory, init_db

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="[worker] %(message)s")
log = logging.getLogger("gigbid.worker")

_ATTEMPTS: dict[str, int] = {}
_SCOUTS: dict[str, Scout] = {}


def _key(user_id: str, job_id: str) -> str:
    return hashlib.sha1(f"{user_id}:{job_id}".encode()).hexdigest()[:16]


def _scout_for(user_id: str, session) -> Scout:
    """De-duplication state is rebuilt from the database on a cold start."""
    if user_id not in _SCOUTS:
        seen_ids = {
            t.job_id for t in session.scalars(select(Thread).where(Thread.user_id == user_id))
        }
        _SCOUTS[user_id] = Scout(seen_ids=seen_ids)
    return _SCOUTS[user_id]


# ---------------------------------------------------------------------------
# Core units of work
# ---------------------------------------------------------------------------
async def analyse_one(user_id: str, job: dict[str, Any]) -> dict[str, Any]:
    """Analyse a single job. Safe to call twice: the thread id is derived."""
    cfg = get_config()
    max_attempts = int(cfg.get("scheduling.dead_letter_after_attempts", 3))
    key = _key(user_id, job.get("job_id", ""))

    if _ATTEMPTS.get(key, 0) >= max_attempts:
        return {"skipped": True, "reason": "dead_letter", "job_id": job.get("job_id")}

    sessions = get_session_factory()
    with sessions() as session:
        user = session.get(User, user_id)
        if user is None:
            return {"error": "unknown user", "user_id": user_id}

        # Deterministic thread id => a restart resumes, never duplicates.
        thread_id = f"t_{key}"
        existing = session.get(Thread, thread_id)
        if existing is not None and existing.status in {"done", "skipped"}:
            return {"skipped": True, "reason": "already_analysed", "thread_id": thread_id}

        await service.start_bid_run(session, user, job, thread_id=thread_id)
        try:
            events = 0
            async for _ in service.stream_bid_run(session, user, job, thread_id):
                events += 1
            _ATTEMPTS.pop(key, None)
            row = session.get(Thread, thread_id)
            return {
                "thread_id": thread_id,
                "job_id": job.get("job_id"),
                "status": row.status if row else "unknown",
                "decision": row.decision if row else "",
                "events": events,
            }
        except Exception as exc:  # noqa: BLE001
            attempts = _ATTEMPTS.get(key, 0) + 1
            _ATTEMPTS[key] = attempts
            log.warning("analysis failed (attempt %s/%s) for %s: %s",
                        attempts, max_attempts, job.get("job_id"), exc)
            row = session.get(Thread, thread_id)
            if row is not None and attempts >= max_attempts:
                row.status = "dead_letter"
                row.terminal_reason = f"Failed {attempts} times: {type(exc).__name__}: {exc}"[:2000]
                session.commit()
            return {"error": str(exc), "attempts": attempts, "job_id": job.get("job_id")}


async def run_saved_searches(user_id: str) -> dict[str, Any]:
    """One scouting pass for one user."""
    cfg = get_config()
    min_interval = float(cfg.get("marketplace.rate_limit.min_scout_interval_seconds", 1800))
    sessions = get_session_factory()
    now = datetime.now(UTC)

    with sessions() as session:
        user = session.get(User, user_id)
        if user is None:
            return {"error": "unknown user"}
        searches = list(
            session.scalars(
                select(SavedSearch).where(
                    SavedSearch.user_id == user_id, SavedSearch.enabled.is_(True)
                )
            )
        )
        due = []
        for search in searches:
            interval = max(float(search.interval_seconds), min_interval)
            last = search.last_run_at
            if last is None or (now - last.replace(tzinfo=UTC)).total_seconds() >= interval:
                due.append(search)

        if not due:
            return {"scouted": 0, "reason": "nothing due", "searches": len(searches)}

        scout = _scout_for(user_id, session)
        payload = [
            {
                "query": s.query,
                "skills": s.skills,
                "budget_min": s.budget_min,
                "limit": 25,
            }
            for s in due
        ]
        result = await scout.run_saved_searches(payload)

        for search in due:
            search.last_run_at = now
        session.commit()

    if result.degraded:
        log.warning("marketplace degraded during scouting: %s", result.errors[:1])
        return {"degraded": True, "errors": result.errors, **result.summary()}

    max_concurrent = int(cfg.get("scheduling.max_concurrent_job_threads", 4))
    semaphore = asyncio.Semaphore(max_concurrent)

    async def bounded(job: dict[str, Any]):
        async with semaphore:
            return await analyse_one(user_id, job)

    analyses = await asyncio.gather(
        *(bounded(job) for job in result.queued), return_exceptions=True
    )
    return {
        **result.summary(),
        "analysed": len([a for a in analyses if isinstance(a, dict) and not a.get("error")]),
        "failed": len([a for a in analyses if not isinstance(a, dict) or a.get("error")]),
    }


async def poll_messages(user_id: str) -> dict[str, Any]:
    """Fetch new client messages and run the conversation graph on each."""
    from agent.marketplace_port import MarketplaceUnavailable, get_marketplace
    from apps.api.db import MessageRecord

    sessions = get_session_factory()
    port = get_marketplace()
    try:
        messages = await port.list_messages()
    except MarketplaceUnavailable as exc:
        return {"degraded": True, "error": str(exc)}
    finally:
        await port.aclose()

    handled = 0
    with sessions() as session:
        user = session.get(User, user_id)
        if user is None:
            return {"error": "unknown user"}
        known = {
            m.marketplace_message_id
            for m in session.scalars(select(MessageRecord).where(MessageRecord.user_id == user_id))
        }
        for message in messages:
            if message.get("sender") != "client" or message.get("message_id") in known:
                continue
            thread_id = f"c_{_key(user_id, message.get('message_id', ''))}"
            async for _ in service.stream_conversation_run(session, user, message, thread_id):
                pass
            handled += 1
    return {"messages_handled": handled}


# ---------------------------------------------------------------------------
# Celery
# ---------------------------------------------------------------------------
def build_celery():
    from celery import Celery

    app = Celery("gigbid", broker=Secrets.redis_url(), backend=Secrets.redis_url())
    app.conf.update(
        task_serializer="json",
        accept_content=["json"],
        result_serializer="json",
        timezone="UTC",
        enable_utc=True,
        task_acks_late=True,            # redelivered if the worker dies mid-task
        worker_prefetch_multiplier=1,   # no hoarding; one long task at a time
        task_time_limit=900,
        task_soft_time_limit=840,
        beat_schedule={
            "scout-all-users": {
                "task": "gigbid.scout_all",
                "schedule": float(
                    get_config().get("scheduling.saved_search_default_interval_seconds", 3600)
                ),
            },
            "poll-messages": {"task": "gigbid.poll_all_messages", "schedule": 900.0},
        },
    )
    return app


try:  # pragma: no cover - import-time branch
    celery_app = build_celery()

    @celery_app.task(name="gigbid.scout_user", bind=True, max_retries=3)
    def scout_user_task(self, user_id: str):
        try:
            return asyncio.run(run_saved_searches(user_id))
        except Exception as exc:  # noqa: BLE001
            raise self.retry(exc=exc, countdown=2 ** self.request.retries * 30) from exc

    @celery_app.task(name="gigbid.analyse_job")
    def analyse_job_task(user_id: str, job: dict[str, Any]):
        return asyncio.run(analyse_one(user_id, job))

    @celery_app.task(name="gigbid.scout_all")
    def scout_all_task():
        init_db()
        with get_session_factory()() as session:
            user_ids = [u.id for u in session.scalars(select(User))]
        return [scout_user_task.delay(uid).id for uid in user_ids]

    @celery_app.task(name="gigbid.poll_all_messages")
    def poll_all_messages_task():
        init_db()
        with get_session_factory()() as session:
            user_ids = [u.id for u in session.scalars(select(User))]
        return [asyncio.run(poll_messages(uid)) for uid in user_ids]

except Exception as exc:  # noqa: BLE001
    celery_app = None
    log.info("Celery unavailable (%s); use `python -m worker.tasks` for the local scheduler.", exc)


# ---------------------------------------------------------------------------
# Broker-free local scheduler
# ---------------------------------------------------------------------------
async def run_forever(interval: float | None = None) -> None:
    init_db()
    cfg = get_config()
    period = interval or float(cfg.get("scheduling.saved_search_default_interval_seconds", 3600))
    log.info("local scheduler started; scouting every %.0fs", period)

    while True:
        started = time.monotonic()
        try:
            with get_session_factory()() as session:
                user_ids = [u.id for u in session.scalars(select(User))]
            for user_id in user_ids:
                result = await run_saved_searches(user_id)
                log.info("scout %s -> %s", user_id[:8], result)
                message_result = await poll_messages(user_id)
                if message_result.get("messages_handled"):
                    log.info("messages %s -> %s", user_id[:8], message_result)
        except Exception as exc:  # noqa: BLE001 - the loop must survive anything
            log.exception("scheduler pass failed: %s", exc)
        await asyncio.sleep(max(5.0, period - (time.monotonic() - started)))


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="GigBid worker (local scheduler)")
    parser.add_argument("--interval", type=float, default=None)
    parser.add_argument("--once", action="store_true", help="run one pass and exit")
    args = parser.parse_args()

    if args.once:
        init_db()
        with get_session_factory()() as s:
            ids = [u.id for u in s.scalars(select(User))]
        for uid in ids:
            print(asyncio.run(run_saved_searches(uid)))
    else:
        asyncio.run(run_forever(args.interval))
