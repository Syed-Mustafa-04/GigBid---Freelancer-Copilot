"""The single marketplace interface (R1).

Everything the graph knows about "the marketplace" is this Protocol. Two
adapters implement it - GigSim and live Upwork - and the graph cannot tell
which one it is talking to. Switching is one line of config.

The port also owns the cross-cutting behaviour that must hold for *both*
adapters, because it is a property of talking to someone else's platform and
not of any one platform:

* a politeness rate limiter that stays under the platform's published limit;
* bounded retry with exponential backoff and jitter;
* a declared degraded mode - when the marketplace is unreachable the port
  raises :class:`MarketplaceUnavailable` rather than returning empty results,
  so the UI can say so and the worker can queue the work (A6);
* an append-only audit record of every write.
"""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


class MarketplaceError(RuntimeError):
    """Base class for marketplace failures."""


class MarketplaceUnavailable(MarketplaceError):
    """The marketplace could not be reached. Callers must degrade visibly."""


class RateLimited(MarketplaceError):
    def __init__(self, retry_after: float) -> None:
        super().__init__(f"rate limited, retry after {retry_after:.1f}s")
        self.retry_after = retry_after


class InsufficientConnects(MarketplaceError):
    """Refusing a write because the Connects budget would be exceeded."""


@dataclass
class AuditEntry:
    ts: float
    user_id: str
    tool: str
    arguments: dict[str, Any]
    result_summary: str
    trace_id: str = ""
    idempotency_key: str = ""


@dataclass
class RateLimiter:
    """Token bucket that stays strictly under the platform's stated limit."""

    max_per_minute: int = 20
    burst: int = 5
    _events: list[float] = field(default_factory=list)

    async def acquire(self) -> None:
        while True:
            now = time.monotonic()
            self._events = [t for t in self._events if now - t < 60.0]
            if len(self._events) < self.max_per_minute:
                recent = [t for t in self._events if now - t < 1.0]
                if len(recent) < self.burst:
                    self._events.append(now)
                    return
                await asyncio.sleep(0.2)
                continue
            sleep_for = 60.0 - (now - self._events[0]) + 0.05
            await asyncio.sleep(max(sleep_for, 0.1))


@runtime_checkable
class MarketplacePort(Protocol):
    """Read and write surface. Implemented by GigSim and live Upwork."""

    name: str

    # -- reads -------------------------------------------------------------
    async def search_jobs(
        self,
        query: str = "",
        *,
        skills: list[str] | None = None,
        budget_min: float | None = None,
        limit: int = 20,
        posted_after: str | None = None,
    ) -> list[dict[str, Any]]: ...

    async def get_job(self, job_id: str) -> dict[str, Any]: ...

    async def get_client_history(self, client_id: str) -> dict[str, Any]: ...

    async def get_market_stats(self, skill: str) -> dict[str, Any]: ...

    async def get_connects_balance(self, freelancer_id: str) -> dict[str, Any]: ...

    async def list_messages(self, thread_id: str | None = None) -> list[dict[str, Any]]: ...

    # -- writes (always after human approval) ------------------------------
    async def create_proposal_draft(
        self, payload: dict[str, Any], *, idempotency_key: str, dry_run: bool = False
    ) -> dict[str, Any]: ...

    async def submit_proposal(
        self, proposal_id: str, *, idempotency_key: str, dry_run: bool = False
    ) -> dict[str, Any]: ...

    async def send_message(
        self, payload: dict[str, Any], *, idempotency_key: str, dry_run: bool = False
    ) -> dict[str, Any]: ...

    async def aclose(self) -> None: ...


class RetryPolicy:
    """Bounded retry with exponential backoff and full jitter."""

    def __init__(
        self,
        max_attempts: int = 3,
        base: float = 0.5,
        cap: float = 8.0,
        retry_on_status: tuple[int, ...] = (429, 500, 502, 503, 504),
    ) -> None:
        self.max_attempts = max_attempts
        self.base = base
        self.cap = cap
        self.retry_on_status = retry_on_status
        self._rng = random.Random(1337)

    def delay(self, attempt: int, retry_after: float | None = None) -> float:
        if retry_after is not None:
            return min(retry_after, self.cap)
        return self._rng.uniform(0, min(self.cap, self.base * (2**attempt)))

    def should_retry(self, status: int | None, attempt: int) -> bool:
        if attempt >= self.max_attempts - 1:
            return False
        return status is None or status in self.retry_on_status


def get_marketplace(config=None, **kwargs) -> MarketplacePort:
    """Factory. The graph calls this and never names an adapter.

    Imported lazily so that selecting GigSim does not require the live Upwork
    MCP dependencies to be installed, and vice versa.
    """
    from agent.config import get_config

    cfg = config or get_config()
    mode = cfg.marketplace_mode

    if mode == "gigsim":
        from mcp_servers.marketplace.adapters.gigsim import GigSimAdapter

        return GigSimAdapter(cfg, **kwargs)
    if mode == "upwork_live":
        from mcp_servers.marketplace.adapters.upwork_live import UpworkLiveAdapter

        return UpworkLiveAdapter(cfg, **kwargs)
    raise ValueError(
        f"Unknown marketplace mode '{mode}'. Set marketplace.mode to 'gigsim' or 'upwork_live'."
    )
