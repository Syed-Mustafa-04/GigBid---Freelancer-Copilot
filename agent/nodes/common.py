"""Shared node machinery: prompt loading, timing, budgets, error containment.

Every node is wrapped by :func:`node` which:

* loads the versioned prompt from ``agent/prompts/`` (never inline),
* times the node and records tokens and cost into state,
* enforces the step and token budgets (C4) by moving the thread to
  ``needs_attention`` rather than letting it run away,
* contains failures: a node that raises records the error, marks the service
  degraded if that is why, and returns a partial update so the graph can keep
  going instead of dying mid-thread (C7).
"""

from __future__ import annotations

import functools
import time
from collections.abc import Callable
from typing import Any

from langgraph.errors import GraphBubbleUp

from agent.config import REPO_ROOT, get_config
from agent.llm import BudgetExceeded, RunLedger, get_llm_client
from agent.mcp_client import ToolForbidden, ToolInvocationError, ToolUnavailable, get_registry

PROMPT_DIR = REPO_ROOT / "agent" / "prompts"
_PROMPT_CACHE: dict[str, str] = {}


def load_prompt(name: str) -> str:
    """Load a versioned prompt file, stripping its YAML front matter."""
    if name in _PROMPT_CACHE:
        return _PROMPT_CACHE[name]
    path = PROMPT_DIR / f"{name}.md"
    if not path.exists():
        raise FileNotFoundError(
            f"Prompt '{name}' not found at {path}. Prompts must live in agent/prompts/, "
            "never inline in Python. Run: python scripts/write_prompts.py"
        )
    text = path.read_text(encoding="utf-8")
    if text.startswith("---"):
        parts = text.split("---", 2)
        text = parts[2] if len(parts) > 2 else text
    _PROMPT_CACHE[name] = text.strip()
    return _PROMPT_CACHE[name]


def prompt_version(name: str) -> int:
    path = PROMPT_DIR / f"{name}.md"
    if not path.exists():
        return 0
    for line in path.read_text(encoding="utf-8").splitlines()[:8]:
        if line.startswith("version:"):
            try:
                return int(line.split(":", 1)[1].strip())
            except ValueError:
                return 0
    return 0


def system_message(node_name: str, extra: str = "") -> dict[str, str]:
    """Shared standing rules plus the node's own prompt."""
    parts = [load_prompt("system_header"), load_prompt(node_name)]
    if extra:
        parts.append(extra)
    return {"role": "system", "content": "\n\n---\n\n".join(parts)}


def ledger_for(state: dict[str, Any], *, kind: str = "job") -> RunLedger:
    """Budget remaining for THIS node, out of the thread-wide allowance.

    The cap is thread-wide on purpose. An earlier version charged drafting
    nodes against ``max_tokens_per_proposal`` while the analysis nodes had
    already spent from the same counter, so the Writer was starved on long
    jobs and the thread died at ``needs_attention`` with a usable analysis and
    no draft. The per-proposal figure is now an accounting line in the report,
    not a second hard limit.
    """
    cfg = get_config()
    budgets = cfg.section("budgets")
    max_tokens = int(budgets.get("max_tokens_per_job", 45000))
    max_usd = float(
        budgets.get("max_usd_per_job" if kind == "job" else "max_usd_per_proposal", 0.05)
    )
    spent = int(state.get("tokens_used", 0))
    remaining = max_tokens - spent
    if remaining <= 0:
        # Hand back a ledger that trips immediately rather than one that is
        # silently unlimited.
        return RunLedger(max_tokens=0, max_usd=max_usd)
    return RunLedger(max_tokens=remaining, max_usd=max_usd)


def node(name: str, *, kind: str = "job") -> Callable:
    """Decorator turning an async function into an instrumented graph node."""

    def decorator(fn: Callable) -> Callable:
        @functools.wraps(fn)
        async def wrapper(state: dict[str, Any], *args, **kwargs) -> dict[str, Any]:
            cfg = get_config()
            started = time.perf_counter()

            # --- step budget -------------------------------------------
            max_steps = int(cfg.get("budgets.max_graph_steps", 40))
            if int(state.get("step_count", 0)) >= max_steps:
                return {
                    "status": "needs_attention",
                    "terminal_reason": f"step budget exhausted ({max_steps} steps)",
                    "errors": [f"{name}: step budget exhausted"],
                    "step_count": 1,
                }

            ledger = ledger_for(state, kind=kind)
            try:
                update = await fn(state, ledger, *args, **kwargs) or {}
            except GraphBubbleUp:
                # interrupt() raises to suspend the graph. It is control flow,
                # not a failure: swallowing it here silently turned every
                # human-approval pause into a logged error and a dead thread.
                raise
            except BudgetExceeded as exc:
                update = {
                    "status": "needs_attention",
                    "terminal_reason": f"budget exceeded in {name}: {exc}",
                    "errors": [f"{name}: {exc}"],
                }
            except ToolForbidden as exc:
                # A programming error, not a runtime condition. Surface loudly.
                update = {
                    "status": "needs_attention",
                    "terminal_reason": f"tool permission error in {name}",
                    "errors": [f"{name}: FORBIDDEN {exc}"],
                }
            except ToolInvocationError as exc:
                update = {
                    "status": "needs_attention",
                    "terminal_reason": f"internal tool contract error in {name}",
                    "errors": [f"{name}: {exc}"],
                }
            except ToolUnavailable as exc:
                update = {
                    "errors": [f"{name}: {exc}"],
                    "degraded_services": [exc.server],
                }
            except Exception as exc:  # noqa: BLE001 - one node must not kill a thread
                update = {"errors": [f"{name}: {type(exc).__name__}: {exc}"]}

            elapsed = time.perf_counter() - started
            snapshot = ledger.snapshot()
            registry = get_registry()
            trace_entries = [
                {**entry, "node": name}
                for entry in registry.trace_dict()[len(state.get("trace", [])) :]
            ]

            merged: dict[str, Any] = {
                "step_count": 1,
                "tokens_used": snapshot["total_tokens"],
                "cost_usd": snapshot["cost_usd"],
                "node_timings": {
                    name: {
                        "seconds": round(elapsed, 4),
                        "tokens": snapshot["total_tokens"],
                        "cost_usd": snapshot["cost_usd"],
                        "llm_calls": snapshot["calls"],
                        "prompt_version": prompt_version(name) if (PROMPT_DIR / f"{name}.md").exists() else None,
                    }
                },
                "trace": [
                    {
                        "node": name,
                        "ts": time.time(),
                        "seconds": round(elapsed, 4),
                        "llm_calls": ledger.calls,
                        "tool_calls": trace_entries,
                    }
                ],
            }
            # Node updates win over the instrumentation defaults.
            for key, value in update.items():
                merged[key] = value
            return merged

        wrapper.node_name = name  # type: ignore[attr-defined]
        return wrapper

    return decorator


async def call_tool(
    agent: str, server: str, tool: str, arguments: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Thin wrapper so nodes never import the registry directly."""
    return await get_registry().call(agent, server, tool, arguments or {})


def llm():
    return get_llm_client()


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, float(value)))
