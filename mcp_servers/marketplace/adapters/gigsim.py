"""GigSim adapter - the development and grading marketplace.

Implements :class:`MarketplacePort` over GigSim's REST API, adding the
behaviour the port guarantees regardless of backend: politeness rate limiting,
bounded retry with backoff, visible degradation and an append-only audit log.
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any

import httpx

from agent.config import Config, get_config
from agent.marketplace_port import (
    AuditEntry,
    InsufficientConnects,
    MarketplaceUnavailable,
    RateLimiter,
    RetryPolicy,
)


class GigSimAdapter:
    name = "gigsim"

    def __init__(
        self,
        config: Config | None = None,
        *,
        base_url: str | None = None,
        token: str | None = None,
        user_id: str = "p_ai_engineer",
    ) -> None:
        self.config = config or get_config()
        self.base_url = (base_url or os.getenv("GIGSIM_BASE_URL", "http://localhost:8081")).rstrip("/")
        self.token = token or os.getenv("GIGSIM_API_TOKEN", "gigsim-dev-token")
        self.user_id = user_id

        rl = self.config.section("marketplace.rate_limit")
        self.limiter = RateLimiter(
            max_per_minute=int(rl.get("max_requests_per_minute", 20)),
            burst=int(rl.get("max_burst", 5)),
        )
        retries = self.config.section("marketplace.retries")
        self.retry = RetryPolicy(
            max_attempts=int(retries.get("max_attempts", 3)),
            base=float(retries.get("backoff_base_seconds", 0.5)),
            cap=float(retries.get("backoff_max_seconds", 8.0)),
            retry_on_status=tuple(retries.get("retry_on_status", [429, 500, 502, 503, 504])),
        )
        timeouts = self.config.section("marketplace.timeouts")
        self._timeout = httpx.Timeout(
            float(timeouts.get("read_seconds", 30.0)),
            connect=float(timeouts.get("connect_seconds", 5.0)),
        )
        self._client: httpx.AsyncClient | None = None
        self.audit: list[AuditEntry] = []
        self.degraded: bool = False
        self.last_error: str = ""

    # -- transport ---------------------------------------------------------
    async def _http(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=self._timeout,
                headers={"Authorization": f"Bearer {self.token}"},
            )
        return self._client

    async def aclose(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        await self.limiter.acquire()
        headers: dict[str, str] = {}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key

        last_detail = ""
        for attempt in range(self.retry.max_attempts):
            try:
                client = await self._http()
                response = await client.request(
                    method, path, params=params, json=json_body, headers=headers
                )
            except (httpx.ConnectError, httpx.ReadTimeout, httpx.ConnectTimeout) as exc:
                last_detail = f"{type(exc).__name__}: {exc}"
                if self.retry.should_retry(None, attempt):
                    await asyncio.sleep(self.retry.delay(attempt))
                    continue
                self.degraded = True
                self.last_error = last_detail
                raise MarketplaceUnavailable(
                    f"GigSim unreachable at {self.base_url}: {last_detail}"
                ) from exc

            if response.status_code < 400:
                self.degraded = False
                self.last_error = ""
                return response.json() if response.content else {}

            if response.status_code == 409:
                raise InsufficientConnects(response.text[:300])

            last_detail = f"HTTP {response.status_code}: {response.text[:200]}"
            if self.retry.should_retry(response.status_code, attempt):
                retry_after = response.headers.get("Retry-After")
                await asyncio.sleep(
                    self.retry.delay(attempt, float(retry_after) if retry_after else None)
                )
                continue

            if response.status_code in (404,):
                raise KeyError(last_detail)

            self.degraded = True
            self.last_error = last_detail
            raise MarketplaceUnavailable(f"GigSim error: {last_detail}")

        self.degraded = True
        self.last_error = last_detail
        raise MarketplaceUnavailable(f"GigSim failed after retries: {last_detail}")

    def _record(self, tool: str, args: dict[str, Any], summary: str, key: str = "") -> None:
        self.audit.append(
            AuditEntry(
                ts=time.time(),
                user_id=self.user_id,
                tool=tool,
                arguments=args,
                result_summary=summary,
                idempotency_key=key,
            )
        )

    # -- reads -------------------------------------------------------------
    async def search_jobs(
        self,
        query: str = "",
        *,
        skills: list[str] | None = None,
        budget_min: float | None = None,
        limit: int = 20,
        posted_after: str | None = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"query": query, "limit": limit}
        if skills:
            params["skills"] = ",".join(skills)
        if budget_min is not None:
            params["budget_min"] = budget_min
        if posted_after:
            params["posted_after"] = posted_after
        data = await self._request("GET", "/jobs", params=params)
        return data.get("jobs", [])

    async def get_job(self, job_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/jobs/{job_id}")

    async def get_client_history(self, client_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/clients/{client_id}/history")

    async def get_market_stats(self, skill: str) -> dict[str, Any]:
        return await self._request("GET", "/market/stats", params={"skill": skill})

    async def get_connects_balance(self, freelancer_id: str | None = None) -> dict[str, Any]:
        return await self._request(
            "GET", "/connects/balance", params={"freelancer_id": freelancer_id or self.user_id}
        )

    async def list_messages(self, thread_id: str | None = None) -> list[dict[str, Any]]:
        params = {"thread_id": thread_id} if thread_id else None
        data = await self._request("GET", "/messages", params=params)
        return data.get("messages", [])

    # -- writes ------------------------------------------------------------
    async def create_proposal_draft(
        self, payload: dict[str, Any], *, idempotency_key: str, dry_run: bool = False
    ) -> dict[str, Any]:
        body = {**payload, "freelancer_id": payload.get("freelancer_id", self.user_id), "dry_run": dry_run}
        result = await self._request(
            "POST", "/proposals", json_body=body, idempotency_key=idempotency_key
        )
        if not dry_run:
            self._record(
                "create_proposal_draft",
                {"job_id": payload.get("job_id")},
                f"proposal={result.get('proposal', {}).get('proposal_id')}",
                idempotency_key,
            )
        return result

    async def submit_proposal(
        self, proposal_id: str, *, idempotency_key: str, dry_run: bool = False
    ) -> dict[str, Any]:
        result = await self._request(
            "POST",
            "/proposals/submit",
            json_body={"proposal_id": proposal_id, "dry_run": dry_run},
            idempotency_key=idempotency_key,
        )
        if not dry_run:
            self._record(
                "submit_proposal",
                {"proposal_id": proposal_id},
                f"connects={result.get('connects_spent')} replay={result.get('idempotent_replay')}",
                idempotency_key,
            )
        return result

    async def send_message(
        self, payload: dict[str, Any], *, idempotency_key: str, dry_run: bool = False
    ) -> dict[str, Any]:
        body = {**payload, "freelancer_id": payload.get("freelancer_id", self.user_id), "dry_run": dry_run}
        result = await self._request(
            "POST", "/messages", json_body=body, idempotency_key=idempotency_key
        )
        if not dry_run:
            self._record(
                "send_message",
                {"thread_id": payload.get("thread_id")},
                f"message={result.get('message', {}).get('message_id')}",
                idempotency_key,
            )
        return result

    # -- health ------------------------------------------------------------
    async def health(self) -> dict[str, Any]:
        try:
            client = await self._http()
            response = await client.get("/health")
            return {"available": response.status_code < 400, **response.json()}
        except Exception as exc:  # noqa: BLE001
            self.degraded = True
            self.last_error = str(exc)
            return {"available": False, "error": str(exc)}
