"""R&D question A: does tool granularity change agent behaviour?

Three tool sets over the *same* portfolio capability:

``fine``    10 narrow tools, one per evidence type and lookup.
``coarse``   3 broad tools with mode and filter arguments.
``best``     4 tools with descriptions rewritten to say WHEN to call them.

40 tasks with a known-correct call. For each, we record whether the agent
picked the right tool and whether it filled the arguments correctly, plus the
tokens the tool schemas themselves consume - which is the hidden cost of the
fine-grained set and the reason "more, clearer tools" is not free.

Scored two ways because they answer different questions:

* **Oracle scoring** is deterministic and runs with no API key: for each task,
  does the tool set even *contain* a tool that can express the task, and does
  the correct call need arguments the schema supports? This measures the design.
* **Model scoring** needs a key: the model is given the tool list and the task
  and asked which tool it would call with which arguments. This measures
  behaviour.

Run: python -m evals.runners.tool_granularity
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent.config import REPO_ROOT
from agent.llm import estimate_tokens, get_llm_client

RESULTS = REPO_ROOT / "evals" / "results"
TOOLSETS = ("fine", "coarse", "best")

# ---------------------------------------------------------------------------
# 40 tasks. `expects` names the capability, mapped per toolset below.
# ---------------------------------------------------------------------------
TASKS: list[dict[str, Any]] = [
    {"id": "t01", "task": "Find proof I have built a recommender system.", "expects": "search", "args": {"query": True}},
    {"id": "t02", "task": "Find evidence for 'integrate with Stripe for subscription billing'.", "expects": "search", "args": {"query": True}},
    {"id": "t03", "task": "Show me the full text of evidence item ev_abc123.", "expects": "get_item", "args": {"id": True}},
    {"id": "t04", "task": "What skills can my portfolio actually evidence?", "expects": "list_skills", "args": {}},
    {"id": "t05", "task": "Have I done churn prediction for a fintech client specifically?", "expects": "find_projects", "args": {"skill": True, "industry": True}},
    {"id": "t06", "task": "Find a past proposal that was WON about dbt.", "expects": "search", "args": {"query": True, "outcome": True}},
    {"id": "t07", "task": "Show me code I wrote for time series forecasting.", "expects": "search", "args": {"query": True, "type": True}},
    {"id": "t08", "task": "Find a client review mentioning communication.", "expects": "search", "args": {"query": True, "type": True}},
    {"id": "t09", "task": "Read the README of the Celery project.", "expects": "search", "args": {"query": True, "type": True}},
    {"id": "t10", "task": "Do I have any e-commerce experience at all?", "expects": "find_projects", "args": {"industry": True}},
    {"id": "t11", "task": "Get case study ev_9f2 in full so I can quote the metric.", "expects": "get_item", "args": {"id": True}},
    {"id": "t12", "task": "Find evidence for 'build a FastAPI service with OpenAPI docs'.", "expects": "search", "args": {"query": True}},
    {"id": "t13", "task": "Which projects prove I know Postgres?", "expects": "find_projects", "args": {"skill": True}},
    {"id": "t14", "task": "Find my strongest evidence for A/B testing.", "expects": "search", "args": {"query": True}},
    {"id": "t15", "task": "Show proposals that got ignored, so I can see what not to do.", "expects": "search", "args": {"query": True, "outcome": True}},
    {"id": "t16", "task": "List every skill with how many projects back it.", "expects": "list_skills", "args": {}},
    {"id": "t17", "task": "Find evidence for 'nightly ETL with idempotent loads'.", "expects": "search", "args": {"query": True}},
    {"id": "t18", "task": "Have I worked in healthcare on NLP?", "expects": "find_projects", "args": {"skill": True, "industry": True}},
    {"id": "t19", "task": "Open evidence ev_441 so I can check the exact numbers.", "expects": "get_item", "args": {"id": True}},
    {"id": "t20", "task": "Find proof of Kubernetes deployment work.", "expects": "search", "args": {"query": True}},
    {"id": "t21", "task": "Which of my projects used Power BI?", "expects": "find_projects", "args": {"skill": True}},
    {"id": "t22", "task": "Find a won proposal in logistics.", "expects": "search", "args": {"query": True, "outcome": True}},
    {"id": "t23", "task": "Show the code behind the churn model.", "expects": "search", "args": {"query": True, "type": True}},
    {"id": "t24", "task": "Find evidence for 'handover documentation and tests'.", "expects": "search", "args": {"query": True}},
    {"id": "t25", "task": "What is in my CV?", "expects": "get_item", "args": {"id": True}},
    {"id": "t26", "task": "Find evidence for 'LangGraph stateful agent with checkpointing'.", "expects": "search", "args": {"query": True}},
    {"id": "t27", "task": "Do I have SaaS domain experience?", "expects": "find_projects", "args": {"industry": True}},
    {"id": "t28", "task": "Find reviews from manufacturing clients.", "expects": "search", "args": {"query": True, "type": True}},
    {"id": "t29", "task": "Get the full case study for project proj_77.", "expects": "get_item", "args": {"id": True}},
    {"id": "t30", "task": "Find evidence for 'reduced p95 latency'.", "expects": "search", "args": {"query": True}},
    {"id": "t31", "task": "List the skills I can claim without lying.", "expects": "list_skills", "args": {}},
    {"id": "t32", "task": "Which projects combined Next.js and Stripe?", "expects": "find_projects", "args": {"skill": True}},
    {"id": "t33", "task": "Find a proposal that got a reply about forecasting.", "expects": "search", "args": {"query": True, "outcome": True}},
    {"id": "t34", "task": "Show me README files mentioning Redis.", "expects": "search", "args": {"query": True, "type": True}},
    {"id": "t35", "task": "Find evidence for 'migrate a monolith to services'.", "expects": "search", "args": {"query": True}},
    {"id": "t36", "task": "Have I done computer vision for manufacturing?", "expects": "find_projects", "args": {"skill": True, "industry": True}},
    {"id": "t37", "task": "Open ev_1234 completely.", "expects": "get_item", "args": {"id": True}},
    {"id": "t38", "task": "Find proof of experiment design work.", "expects": "search", "args": {"query": True}},
    {"id": "t39", "task": "Which projects can prove transformers experience?", "expects": "find_projects", "args": {"skill": True}},
    {"id": "t40", "task": "Find evidence for 'weekly executive reporting'.", "expects": "search", "args": {"query": True}},
]

# Which concrete tool satisfies each capability, per toolset.
CAPABILITY_MAP: dict[str, dict[str, list[str]]] = {
    "best": {
        "search": ["search_evidence"],
        "get_item": ["get_case_study"],
        "list_skills": ["list_skills"],
        "find_projects": ["find_projects"],
    },
    "coarse": {
        "search": ["search_portfolio"],
        "get_item": ["get_item"],
        "list_skills": ["describe_portfolio"],
        # No structured join exists: the agent must approximate with a search.
        "find_projects": ["search_portfolio", "describe_portfolio"],
    },
    "fine": {
        "search": [
            "search_case_studies", "search_repos", "search_code",
            "search_reviews", "search_proposals", "search_won_proposals",
        ],
        "get_item": ["get_case_study", "get_repo", "get_cv"],
        "list_skills": ["list_skills"],
        # Also unavailable as a join.
        "find_projects": ["search_case_studies", "list_skills"],
    },
}

# Capabilities a toolset cannot express exactly, only approximate.
EXACT: dict[str, set[str]] = {
    "best": {"search", "get_item", "list_skills", "find_projects"},
    "coarse": {"search", "get_item", "list_skills"},
    "fine": {"search", "get_item", "list_skills"},
}

# Argument support per toolset (filters the task needs).
ARG_SUPPORT: dict[str, set[str]] = {
    "best": {"query", "id", "type", "outcome", "skill", "industry"},
    "coarse": {"query", "id", "type", "outcome", "industry"},  # no `skill` join
    "fine": {"query", "id"},  # type is encoded in the tool name; no outcome/industry filters
}


async def load_tools(toolset: str) -> list[dict[str, Any]]:
    """Import the portfolio server under a toolset and list its tools."""
    os.environ["GIGBID_TOOLSET"] = toolset
    import mcp_servers.portfolio.server as server

    importlib.reload(server)
    tools = await server.mcp.list_tools()
    return [
        {
            "name": t.name,
            "description": (t.description or "").strip(),
            "input_schema": t.inputSchema,
        }
        for t in tools
    ]


def oracle_score(toolset: str) -> dict[str, Any]:
    """Deterministic: can this toolset express each task exactly?"""
    rows = []
    for task in TASKS:
        capability = task["expects"]
        exact = capability in EXACT[toolset]
        needed_args = set(task["args"])
        args_ok = needed_args.issubset(ARG_SUPPORT[toolset])
        candidates = CAPABILITY_MAP[toolset][capability]
        rows.append({
            "task_id": task["id"],
            "capability": capability,
            "expressible_exactly": exact,
            "arguments_supported": args_ok,
            "candidate_tools": len(candidates),
            # With several plausible tools and no filter args, the agent has to
            # guess. 1/n is the uninformed-choice baseline.
            "selection_difficulty": round(1 / len(candidates), 3),
        })
    n = len(rows)
    return {
        "exact_expressible_rate": round(sum(r["expressible_exactly"] for r in rows) / n, 4),
        "argument_support_rate": round(sum(r["arguments_supported"] for r in rows) / n, 4),
        "mean_candidates_per_task": round(sum(r["candidate_tools"] for r in rows) / n, 2),
        "uninformed_selection_accuracy": round(
            sum(r["selection_difficulty"] for r in rows) / n, 4
        ),
        "rows": rows,
    }


async def model_score(toolset: str, tools: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Ask the model which tool it would call. Needs an API key."""
    client = get_llm_client()
    if client.is_offline:
        return None

    from pydantic import BaseModel, Field

    class Choice(BaseModel):
        tool_name: str = Field(description="Exact name of the tool you would call")
        arguments: dict[str, Any] = Field(default_factory=dict)

    catalogue = "\n\n".join(
        f"{t['name']}({', '.join((t['input_schema'].get('properties') or {}).keys())})\n{t['description']}"
        for t in tools
    )
    valid = {t["name"] for t in tools}
    correct_tool = 0
    correct_args = 0
    rows = []

    for task in TASKS:
        messages = [
            {
                "role": "system",
                "content": (
                    "You pick exactly one tool to satisfy a request about a freelancer's "
                    "portfolio. Reply with the tool name and the arguments you would pass.\n\n"
                    f"AVAILABLE TOOLS:\n{catalogue}"
                ),
            },
            {"role": "user", "content": task["task"]},
        ]
        try:
            choice, _ = await client.structured("scout", messages, Choice)
            picked = choice.tool_name.strip()
            acceptable = set(CAPABILITY_MAP[toolset][task["expects"]])
            tool_ok = picked in acceptable and picked in valid
            needed = set(task["args"])
            supplied = {k for k, v in choice.arguments.items() if v not in (None, "", [], {})}
            args_ok = tool_ok and needed.issubset(supplied | {"type"} if toolset == "fine" else supplied)
            correct_tool += tool_ok
            correct_args += args_ok
            rows.append({
                "task_id": task["id"], "picked": picked,
                "acceptable": sorted(acceptable), "tool_ok": tool_ok, "args_ok": args_ok,
            })
        except Exception as exc:  # noqa: BLE001
            rows.append({"task_id": task["id"], "error": str(exc)[:120], "tool_ok": False, "args_ok": False})

    n = len(TASKS)
    return {
        "tool_selection_accuracy": round(correct_tool / n, 4),
        "argument_accuracy": round(correct_args / n, 4),
        "rows": rows,
    }


async def main() -> None:
    parser = argparse.ArgumentParser(description="R&D question A: tool granularity")
    parser.add_argument("--out", type=Path, default=RESULTS / "tool_granularity.json")
    args = parser.parse_args()

    client = get_llm_client()
    results: dict[str, Any] = {}

    for toolset in TOOLSETS:
        tools = await load_tools(toolset)
        schema_tokens = estimate_tokens(json.dumps(tools))
        description_tokens = estimate_tokens(" ".join(t["description"] for t in tools))
        oracle = oracle_score(toolset)
        model = await model_score(toolset, tools)

        results[toolset] = {
            "n_tools": len(tools),
            "tool_names": sorted(t["name"] for t in tools),
            "schema_tokens_per_call": schema_tokens,
            "description_tokens": description_tokens,
            # The real per-task cost: the schemas are resent on every call.
            "schema_tokens_per_task": schema_tokens,
            "oracle": {k: v for k, v in oracle.items() if k != "rows"},
            "model": {k: v for k, v in (model or {}).items() if k != "rows"} if model else None,
            "detail": {"oracle_rows": oracle["rows"], "model_rows": (model or {}).get("rows")},
        }
        print(
            f"  {toolset:7s} tools={len(tools):2d} schema_tokens={schema_tokens:5d} "
            f"exact={oracle['exact_expressible_rate']:.2f} "
            f"args={oracle['argument_support_rate']:.2f} "
            f"candidates/task={oracle['mean_candidates_per_task']:.2f}"
            + (f" model_tool_acc={model['tool_selection_accuracy']:.2f}" if model else " (model: skipped, no key)")
        )

    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "n_tasks": len(TASKS),
        "provider": "offline_stub" if client.is_offline else "live_model",
        "model_scoring_available": not client.is_offline,
        "results": results,
        "conclusion_inputs": {
            "schema_token_ratio_fine_vs_best": round(
                results["fine"]["schema_tokens_per_call"]
                / max(results["best"]["schema_tokens_per_call"], 1), 3
            ),
            "exact_expressible_best": results["best"]["oracle"]["exact_expressible_rate"],
            "exact_expressible_coarse": results["coarse"]["oracle"]["exact_expressible_rate"],
            "exact_expressible_fine": results["fine"]["oracle"]["exact_expressible_rate"],
        },
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"\nwritten: {args.out}")
    if client.is_offline:
        print("NOTE: model-behaviour scoring was skipped (no API key). Oracle scores are design "
              "measurements and are valid; tool-selection accuracy needs a key.")


if __name__ == "__main__":
    asyncio.run(main())
