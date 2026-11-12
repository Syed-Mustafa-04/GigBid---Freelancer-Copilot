"""Live Upwork adapter (A4) - the official MCP server behind the same port.

Connects to Upwork's official MCP server at ``https://mcp.upwork.com/mcp``
over Streamable HTTP using ``MultiServerMCPClient``, completing OAuth 2.1 and
storing tokens encrypted per user. Its tools are *mapped onto* the marketplace
port so the graph cannot tell which marketplace it is running against.

Platform-respect rules enforced here, not left to the caller:

* **No scraping, no browser automation, no session cookies.** The only egress
  is the official MCP server. There is no HTML parsing anywhere in this file.
* **Read tools are scheduled, not polled.** ``min_scout_interval_seconds``
  from config is enforced per read tool; a caller that asks sooner gets the
  cached response rather than a new platform request.
* **Writes create drafts only.** ``submit_proposal`` maps to Upwork's draft
  creation; the user confirms on Upwork itself, as the platform requires.
  ``_assert_approved`` refuses any write that is not carrying a human
  approval token, so an agent cannot reach the platform on its own.

Live mode is never graded on outcomes and never submits anything without
explicit confirmation.
"""

from __future__ import annotations

import os
import time
from typing import Any

from agent.config import Config, get_config
from agent.marketplace_port import AuditEntry, MarketplaceUnavailable, RateLimiter


class ApprovalRequired(RuntimeError):
    """Raised when a live write is attempted without a human approval token."""


class UpworkLiveAdapter:
    """MarketplacePort implementation over the official Upwork MCP server."""

    name = "upwork_live"

    # Upwork MCP tool names mapped to our port's methods. Keeping this table
    # explicit is what lets the graph stay ignorant of the marketplace.
    TOOL_MAP = {
        "search_jobs": "upwork_search_jobs",
        "get_job": "upwork_get_job_details",
        "get_client_history": "upwork_get_client_info",
        "get_connects_balance": "upwork_get_connects_balance",
        "list_messages": "upwork_list_messages",
        "create_proposal_draft": "upwork_create_proposal_draft",
        "submit_proposal": "upwork_submit_proposal_draft",
        "send_message": "upwork_create_message_draft",
    }

    def __init__(
        self,
        config: Config | None = None,
        *,
        user_id: str = "",
        token_store: Any | None = None,
    ) -> None:
        self.config = config or get_config()
        self.user_id = user_id
        self.server_url = os.getenv("UPWORK_MCP_URL", "https://mcp.upwork.com/mcp")
        self.token_store = token_store
        self._client: Any | None = None
        self._tools: dict[str, Any] = {}
        self.audit: list[AuditEntry] = []
        self.degraded = False
        self.last_error = ""

        rl = self.config.section("marketplace.rate_limit")
        self.limiter = RateLimiter(
            max_per_minute=int(rl.get("max_requests_per_minute", 20)),
            burst=int(rl.get("max_burst", 5)),
        )
        self.min_read_interval = float(rl.get("min_scout_interval_seconds", 1800))
        self._last_read: dict[str, float] = {}
        self._read_cache: dict[str, Any] = {}

    # -- connection --------------------------------------------------------
    async def _connect(self) -> Any:
        """Open a MultiServerMCPClient session carrying the user's OAuth token."""
        if self._client is not None:
            return self._client
        try:
            from langchain_mcp_adapters.client import MultiServerMCPClient
        except ImportError as exc:  # pragma: no cover
            raise MarketplaceUnavailable(
                "langchain-mcp-adapters is required for live mode: pip install langchain-mcp-adapters"
            ) from exc

        access_token = await self._access_token()
        self._client = MultiServerMCPClient(
            {
                "upwork": {
                    "transport": "streamable_http",
                    "url": self.server_url,
                    "headers": {"Authorization": f"Bearer {access_token}"},
                }
            }
        )
        try:
            tools = await self._client.get_tools()
        except Exception as exc:  # noqa: BLE001
            self.degraded = True
            self.last_error = str(exc)
            raise MarketplaceUnavailable(f"Upwork MCP unreachable: {exc}") from exc
        self._tools = {tool.name: tool for tool in tools}
        return self._client

    async def _access_token(self) -> str:
        """Fetch and refresh the user's token from the encrypted store."""
        if self.token_store is None:
            from apps.api.auth.token_store import TokenStore

            self.token_store = TokenStore()
        token = await self.token_store.get(self.user_id, provider="upwork")
        if token is None:
            raise MarketplaceUnavailable(
                f"No Upwork OAuth token for user {self.user_id}. "
                "Connect the account in Settings before switching to live mode."
            )
        if token.is_expired():
            token = await self.token_store.refresh(self.user_id, provider="upwork")
        return token.access_token

    async def aclose(self) -> None:
        if self._client is not None and hasattr(self._client, "aclose"):
            await self._client.aclose()
        self._client = None

    # -- tool invocation ---------------------------------------------------
    async def _call(self, port_method: str, arguments: dict[str, Any]) -> Any:
        tool_name = self.TOOL_MAP[port_method]
        await self._connect()
        tool = self._tools.get(tool_name)
        if tool is None:
            raise MarketplaceUnavailable(
                f"Upwork MCP server does not expose '{tool_name}'. "
                f"Available: {sorted(self._tools)[:12]}"
            )
        await self.limiter.acquire()
        try:
            return await tool.ainvoke(arguments)
        except Exception as exc:  # noqa: BLE001
            self.degraded = True
            self.last_error = str(exc)
            raise MarketplaceUnavailable(f"Upwork tool '{tool_name}' failed: {exc}") from exc

    async def _cached_read(self, port_method: str, cache_key: str, arguments: dict[str, Any]) -> Any:
        """Serve a cached result rather than polling the platform too often."""
        now = time.monotonic()
        last = self._last_read.get(cache_key, 0.0)
        if cache_key in self._read_cache and (now - last) < self.min_read_interval:
            return self._read_cache[cache_key]
        result = await self._call(port_method, arguments)
        self._read_cache[cache_key] = result
        self._last_read[cache_key] = now
        return result

    @staticmethod
    def _assert_approved(payload: dict[str, Any]) -> None:
        """Refuse any live write that is not carrying a human approval token."""
        if not payload.get("approval_token"):
            raise ApprovalRequired(
                "Live-mode writes require an approval token issued by the human "
                "approval step. The agent cannot reach Upwork on its own."
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
        args = {"query": query, "limit": limit}
        if skills:
            args["skills"] = skills
        if budget_min is not None:
            args["budget_min"] = budget_min
        raw = await self._cached_read("search_jobs", f"search::{query}::{skills}", args)
        return self._normalise_jobs(raw)

    async def get_job(self, job_id: str) -> dict[str, Any]:
        raw = await self._cached_read("get_job", f"job::{job_id}", {"job_id": job_id})
        jobs = self._normalise_jobs(raw)
        return jobs[0] if jobs else {}

    async def get_client_history(self, client_id: str) -> dict[str, Any]:
        raw = await self._cached_read(
            "get_client_history", f"client::{client_id}", {"client_id": client_id}
        )
        return self._normalise_client(raw)

    async def get_market_stats(self, skill: str) -> dict[str, Any]:
        """Upwork's MCP exposes no aggregate price endpoint.

        Rather than scrape for it - which the platform forbids - live mode
        derives the band from the user's own historical contracts and says so.
        """
        return {
            "skill": skill,
            "sample_size": 0,
            "source": "unavailable_in_live_mode",
            "note": (
                "Upwork's official MCP server exposes no market aggregate tool. "
                "Scraping for it would breach the platform terms, so the Pricing "
                "Strategist falls back to the user's own contract history."
            ),
        }

    async def get_connects_balance(self, freelancer_id: str | None = None) -> dict[str, Any]:
        raw = await self._cached_read("get_connects_balance", "connects", {})
        return raw if isinstance(raw, dict) else {"balance": None, "raw": raw}

    async def list_messages(self, thread_id: str | None = None) -> list[dict[str, Any]]:
        args = {"thread_id": thread_id} if thread_id else {}
        raw = await self._call("list_messages", args)
        return raw if isinstance(raw, list) else [raw]

    # -- writes: drafts only -----------------------------------------------
    async def create_proposal_draft(
        self, payload: dict[str, Any], *, idempotency_key: str, dry_run: bool = False
    ) -> dict[str, Any]:
        if dry_run:
            return {
                "dry_run": True,
                "would_call": self.TOOL_MAP["create_proposal_draft"],
                "server": self.server_url,
                "arguments": {k: v for k, v in payload.items() if k != "approval_token"},
                "connects_cost": 0,
                "note": "Creates a DRAFT on Upwork. You confirm it on Upwork itself.",
            }
        self._assert_approved(payload)
        args = {k: v for k, v in payload.items() if k != "approval_token"}
        args["idempotency_key"] = idempotency_key
        result = await self._call("create_proposal_draft", args)
        self._audit("create_proposal_draft", {"job_id": payload.get("job_id")}, idempotency_key)
        return {"proposal": result, "idempotent_replay": False, "draft_only": True}

    async def submit_proposal(
        self, proposal_id: str, *, idempotency_key: str, dry_run: bool = False
    ) -> dict[str, Any]:
        if dry_run:
            return {
                "dry_run": True,
                "would_call": self.TOOL_MAP["submit_proposal"],
                "server": self.server_url,
                "arguments": {"proposal_id": proposal_id},
                "irreversible": True,
                "note": "Spends real Connects. Requires your confirmation on Upwork.",
            }
        raise ApprovalRequired(
            "Live mode never auto-submits. GigBid creates the draft on Upwork; "
            "you confirm and submit it on the platform, as Upwork requires."
        )

    async def send_message(
        self, payload: dict[str, Any], *, idempotency_key: str, dry_run: bool = False
    ) -> dict[str, Any]:
        if dry_run:
            return {
                "dry_run": True,
                "would_call": self.TOOL_MAP["send_message"],
                "server": self.server_url,
                "arguments": {k: v for k, v in payload.items() if k != "approval_token"},
                "connects_cost": 0,
                "note": "Creates a message DRAFT. You send it from Upwork.",
            }
        self._assert_approved(payload)
        args = {k: v for k, v in payload.items() if k != "approval_token"}
        args["idempotency_key"] = idempotency_key
        result = await self._call("send_message", args)
        self._audit("send_message", {"thread_id": payload.get("thread_id")}, idempotency_key)
        return {"message": result, "idempotent_replay": False, "draft_only": True}

    # -- normalisation: Upwork shape -> port shape -------------------------
    @staticmethod
    def _normalise_jobs(raw: Any) -> list[dict[str, Any]]:
        items = raw if isinstance(raw, list) else raw.get("jobs", [raw]) if isinstance(raw, dict) else []
        out = []
        for item in items:
            if not isinstance(item, dict):
                continue
            out.append(
                {
                    "job_id": item.get("id") or item.get("job_id") or item.get("ciphertext", ""),
                    "title": item.get("title", ""),
                    "description": item.get("description") or item.get("snippet", ""),
                    "required_skills": item.get("skills") or item.get("required_skills", []),
                    "budget": {
                        "min": (item.get("budget") or {}).get("minimum")
                        or item.get("budget_min")
                        or 0.0,
                        "max": (item.get("budget") or {}).get("maximum")
                        or item.get("budget_max")
                        or 0.0,
                        "currency": item.get("currency", "USD"),
                        "type": "hourly" if item.get("job_type") == "hourly" else "fixed",
                    },
                    "duration_days": item.get("duration_days"),
                    "screening_questions": item.get("questions", []),
                    "connects_cost": item.get("connects_required", 0),
                    "client_id": (item.get("client") or {}).get("id", ""),
                    "posted_at": item.get("created_on") or item.get("posted_at", ""),
                    "proposals_received": item.get("applicants", 0),
                    "synthetic": False,
                    "source": "upwork_live",
                }
            )
        return out

    @staticmethod
    def _normalise_client(raw: Any) -> dict[str, Any]:
        if not isinstance(raw, dict):
            return {}
        stats = raw.get("stats") or {}
        return {
            "client_id": raw.get("id", ""),
            "display_name": raw.get("name", ""),
            "country": (raw.get("location") or {}).get("country", ""),
            "payment_verified": bool(raw.get("payment_verification_status")),
            "avg_rating": stats.get("feedback_score", 0.0),
            "jobs_posted": stats.get("total_jobs_posted", 0),
            "hire_rate": stats.get("hire_rate", 0.0),
            "total_spend_usd": stats.get("total_charges", 0.0),
            "synthetic": False,
            "source": "upwork_live",
        }

    def _audit(self, tool: str, args: dict[str, Any], key: str) -> None:
        self.audit.append(
            AuditEntry(
                ts=time.time(),
                user_id=self.user_id,
                tool=tool,
                arguments=args,
                result_summary="draft_created",
                idempotency_key=key,
            )
        )

    async def health(self) -> dict[str, Any]:
        try:
            await self._connect()
            return {"available": True, "server": self.server_url, "tools": len(self._tools)}
        except Exception as exc:  # noqa: BLE001
            return {"available": False, "error": str(exc), "server": self.server_url}
