"""MCP client discipline (A6): per-agent allowlists, timeouts, degraded mode.

Two rules this module exists to enforce:

1. **Each agent gets only the tools it needs.** The Job Analyst has no
   marketplace tools at all; the Critic can read the portfolio but cannot write
   anywhere; only the Client Liaison can reach a write tool, and only after
   approval. Filtering by name at the client is a real control, not
   documentation - a node physically cannot call what it was not handed.

2. **A dead server degrades visibly.** Kill the marketplace server mid-run and
   calls raise :class:`ToolUnavailable` carrying a user-facing message. Nodes
   catch it, mark the thread degraded and keep going where they can; the UI
   shows a banner rather than an empty list. "No jobs found" and "the
   marketplace is down" must never look the same to the user.

Transports: ``inprocess`` (default - same Python process, no subprocess, which
is what the API and the eval harness use), ``stdio``, ``streamable-http``.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass, field
from typing import Any

from agent.config import get_config

# ---------------------------------------------------------------------------
# Per-agent tool allowlists. This table is the security boundary.
# ---------------------------------------------------------------------------
AGENT_TOOLS: dict[str, dict[str, list[str]]] = {
    "scout": {
        "marketplace": ["search_jobs", "get_job"],
    },
    "analyst": {},  # pure reasoning over text already fetched - no tools at all
    "risk_assessor": {
        "marketplace": ["get_client_history"],
    },
    "fit_matcher": {
        "portfolio": ["search_evidence", "get_case_study", "list_skills", "find_projects"],
    },
    "pricing": {
        "marketplace": ["get_market_rates", "get_connects_balance", "get_client_history"],
    },
    "writer": {
        "portfolio": ["search_evidence", "get_case_study"],
    },
    "critic": {
        "portfolio": ["search_evidence", "get_case_study"],
    },
    "compliance": {},  # policy pack is loaded data, not a tool call
    "client_liaison": {
        "marketplace": ["list_messages", "get_job", "send_message"],
        "workspace": ["find_free_slots", "create_tentative_hold", "create_email_draft"],
    },
    "submitter": {
        # Only reachable from the post-approval path.
        "marketplace": ["create_proposal_draft", "submit_proposal"],
    },
}

SERVER_MODULES = {
    "portfolio": "mcp_servers.portfolio.server",
    "marketplace": "mcp_servers.marketplace.server",
    "workspace": "mcp_servers.workspace.server",
}


class ToolUnavailable(RuntimeError):
    """A tool could not be reached. Carries a message safe to show the user."""

    def __init__(self, server: str, tool: str, detail: str) -> None:
        super().__init__(f"{server}.{tool}: {detail}")
        self.server = server
        self.tool = tool
        self.detail = detail
        self.user_message = (
            f"The {server} service is not responding. Your work is queued and will "
            f"resume automatically - nothing has been lost."
        )


class ToolForbidden(PermissionError):
    """An agent asked for a tool outside its allowlist."""


class ToolInvocationError(RuntimeError):
    """The call reached the tool and the tool rejected it.

    Kept distinct from :class:`ToolUnavailable` on purpose. A bad argument is a
    defect in our code that retrying cannot fix, and reporting it as an outage
    sends the user to check a service that is working perfectly.
    """

    def __init__(self, server: str, tool: str, detail: str) -> None:
        super().__init__(f"{server}.{tool} rejected the call: {detail}")
        self.server = server
        self.tool = tool
        self.detail = detail
        self.user_message = (
            "GigBid sent an invalid request to an internal tool. This is a bug, "
            "not an outage; the run has been stopped rather than retried."
        )


@dataclass
class ToolCall:
    """One entry in the execution trace (C2)."""

    agent: str
    server: str
    tool: str
    arguments: dict[str, Any]
    result_preview: str
    latency_s: float
    ok: bool
    error: str = ""
    ts: float = field(default_factory=time.time)

    @staticmethod
    def _redact(value: Any, *, limit: int = 600) -> Any:
        """Mask and bound a single argument value, preserving JSON validity.

        An earlier version serialised the whole argument dict, truncated the
        STRING at 1200 characters and re-parsed it. That produces invalid JSON
        the moment the arguments are large - a proposal draft always is - and
        it crashed the approval node mid-run with
        `JSONDecodeError: Expecting value`. Truncation has to happen per value,
        not across the serialised form.
        """
        from agent.safety import mask_pii

        if isinstance(value, str):
            masked = mask_pii(value)
            return masked if len(masked) <= limit else masked[:limit] + "…[truncated]"
        if isinstance(value, dict):
            return {k: ToolCall._redact(v, limit=limit) for k, v in list(value.items())[:40]}
        if isinstance(value, (list, tuple)):
            return [ToolCall._redact(v, limit=limit) for v in list(value)[:20]]
        if isinstance(value, (int, float, bool)) or value is None:
            return value
        return ToolCall._redact(str(value), limit=limit)

    def to_dict(self) -> dict[str, Any]:
        from agent.safety import mask_pii

        return {
            "agent": self.agent,
            "server": self.server,
            "tool": self.tool,
            "arguments": self._redact(self.arguments) if self.arguments else {},
            "result_preview": mask_pii(self.result_preview)[:400],
            "latency_s": round(self.latency_s, 4),
            "ok": self.ok,
            "error": self.error[:200],
            "ts": self.ts,
        }


class MCPToolRegistry:
    """Connects to the GigBid MCP servers and hands out filtered tool sets."""

    def __init__(self, transport: str | None = None) -> None:
        self.transport = transport or os.getenv("GIGBID_MCP_TRANSPORT_MODE", "inprocess")
        self.config = get_config()
        self._servers: dict[str, Any] = {}
        self._failed: dict[str, str] = {}
        self.trace: list[ToolCall] = []
        self.timeout = float(self.config.get("marketplace.timeouts.read_seconds", 30.0))

    # -- server resolution -------------------------------------------------
    def _server(self, name: str) -> Any:
        if name in self._servers:
            return self._servers[name]
        if name in self._failed:
            raise ToolUnavailable(name, "*", self._failed[name])
        module_path = SERVER_MODULES.get(name)
        if module_path is None:
            raise ToolForbidden(f"Unknown MCP server '{name}'")
        try:
            import importlib

            module = importlib.import_module(module_path)
            server = module.mcp
        except Exception as exc:  # noqa: BLE001
            self._failed[name] = f"{type(exc).__name__}: {exc}"
            raise ToolUnavailable(name, "*", str(exc)) from exc
        self._servers[name] = server
        return server

    def mark_down(self, name: str, reason: str = "manually stopped") -> None:
        """Used by the viva demo and the degraded-mode test to kill a server."""
        self._servers.pop(name, None)
        self._failed[name] = reason

    def revive(self, name: str) -> None:
        self._failed.pop(name, None)

    # -- allowlist ---------------------------------------------------------
    @staticmethod
    def tools_for(agent: str) -> dict[str, list[str]]:
        return AGENT_TOOLS.get(agent, {})

    def _assert_allowed(self, agent: str, server: str, tool: str) -> None:
        allowed = AGENT_TOOLS.get(agent, {})
        if server not in allowed or tool not in allowed[server]:
            raise ToolForbidden(
                f"Agent '{agent}' may not call {server}.{tool}. "
                f"Allowed: {json.dumps(allowed)}"
            )

    # -- invocation --------------------------------------------------------
    async def call(
        self,
        agent: str,
        server: str,
        tool: str,
        arguments: dict[str, Any] | None = None,
        *,
        retries: int = 2,
    ) -> Any:
        """Call one MCP tool, enforcing the allowlist and recording the trace."""
        self._assert_allowed(agent, server, tool)
        args = arguments or {}
        started = time.perf_counter()
        last_error = ""

        for attempt in range(retries + 1):
            try:
                instance = self._server(server)
                raw = await asyncio.wait_for(instance.call_tool(tool, args), timeout=self.timeout)
                payload = self._unwrap(raw)
                self.trace.append(
                    ToolCall(agent, server, tool, args, json.dumps(payload, default=str)[:400],
                             time.perf_counter() - started, True)
                )
                return payload
            except ToolForbidden:
                raise
            except KeyError as exc:
                # A missing record is a definite answer, not a transient fault.
                # Retrying it wastes the budget and ends up reported as an
                # outage, which is the one thing degraded mode must not blur.
                self.trace.append(
                    ToolCall(agent, server, tool, args, "", time.perf_counter() - started,
                             False, f"not_found: {exc}")
                )
                return {"error": "not_found", "detail": str(exc)[:200], "degraded": False}
            except TimeoutError:
                last_error = f"timed out after {self.timeout}s"
            except ToolUnavailable as exc:
                last_error = exc.detail
                break
            except Exception as exc:  # noqa: BLE001
                name = type(exc).__name__
                if "Validation" in name or "ToolError" in name or isinstance(exc, TypeError):
                    detail = f"{name}: {exc}"
                    self.trace.append(
                        ToolCall(agent, server, tool, args, "",
                                 time.perf_counter() - started, False, detail)
                    )
                    raise ToolInvocationError(server, tool, detail) from exc
                last_error = f"{name}: {exc}"
            if attempt < retries:
                await asyncio.sleep(min(0.4 * (2**attempt), 3.0))

        self.trace.append(
            ToolCall(agent, server, tool, args, "", time.perf_counter() - started, False, last_error)
        )
        raise ToolUnavailable(server, tool, last_error)

    @staticmethod
    def _unwrap(raw: Any) -> Any:
        """FastMCP returns content blocks or a structured dict. Normalise both."""
        if isinstance(raw, dict):
            return raw
        if isinstance(raw, (list, tuple)):
            if len(raw) == 2 and isinstance(raw[1], dict):
                raw = raw[0]  # (content, structured) pair
            texts = []
            for block in raw:
                text = getattr(block, "text", None)
                if text is not None:
                    texts.append(text)
            joined = "\n".join(texts)
            try:
                return json.loads(joined)
            except (json.JSONDecodeError, ValueError):
                return {"text": joined}
        text = getattr(raw, "text", None)
        if text is not None:
            try:
                return json.loads(text)
            except (json.JSONDecodeError, ValueError):
                return {"text": text}
        return raw

    # -- introspection -----------------------------------------------------
    async def list_tools(self, server: str) -> list[dict[str, Any]]:
        instance = self._server(server)
        tools = await instance.list_tools()
        return [
            {
                "name": t.name,
                "description": (t.description or "").strip(),
                "input_schema": t.inputSchema,
                "annotations": t.annotations.model_dump() if t.annotations else {},
            }
            for t in tools
        ]

    async def schema_snapshot(self) -> dict[str, Any]:
        """Snapshot of every tool signature, for the CI contract test (A7)."""
        snapshot: dict[str, Any] = {}
        for name in SERVER_MODULES:
            try:
                tools = await self.list_tools(name)
            except ToolUnavailable:
                continue
            snapshot[name] = {
                t["name"]: {
                    "input_schema": t["input_schema"],
                    "annotations": t["annotations"],
                }
                for t in sorted(tools, key=lambda x: x["name"])
            }
        return snapshot

    def trace_dict(self) -> list[dict[str, Any]]:
        return [call.to_dict() for call in self.trace]

    def degraded_servers(self) -> list[str]:
        return sorted(self._failed)

    async def aclose(self) -> None:
        self._servers.clear()


_REGISTRY: MCPToolRegistry | None = None


def get_registry() -> MCPToolRegistry:
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = MCPToolRegistry()
    return _REGISTRY


def reset_registry() -> None:
    global _REGISTRY
    _REGISTRY = None
