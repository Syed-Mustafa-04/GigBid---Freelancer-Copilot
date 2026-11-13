"""Generate docs/TOOLS.md from the live MCP servers.

Written by a script rather than by hand so the table cannot drift from the
code. CI regenerates it and fails if the committed file differs, which means a
tool added without documentation breaks the build.

Run: python -m scripts.generate_tools_doc
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from agent.config import REPO_ROOT
from agent.mcp_client import AGENT_TOOLS, MCPToolRegistry

OUT = REPO_ROOT / "docs" / "TOOLS.md"

SERVER_NOTES = {
    "portfolio": (
        "Read-only access to the freelancer's own evidence. No tool on this "
        "server changes anything, so it is safe to expose to any MCP host. "
        "Published separately so another freelancer can point Claude Desktop "
        "or Cursor at their own portfolio."
    ),
    "marketplace": (
        "The only path to a marketplace. Wraps the marketplace port, so the "
        "same tools serve GigSim and live Upwork and the agent cannot tell "
        "which. Every write takes an idempotency key, supports `dry_run`, and "
        "refuses without a human approval token."
    ),
    "workspace": (
        "Calendar and email. Holds are tentative and emails are drafts: "
        "nothing here commits the user to a meeting or sends mail on their "
        "behalf."
    ),
}

TRUST = {
    "portfolio": "First-party. Reads only local, user-owned data. No network egress.",
    "marketplace": "First-party. Network egress to GigSim or the official Upwork MCP server only.",
    "workspace": "First-party. Local mock by default; Google Calendar/Gmail via their official APIs when enabled.",
    "upwork_official": (
        "Third-party, operated by Upwork. Trusted because it is the platform's own "
        "server and the only sanctioned automation route; scraping is a terms "
        "violation. OAuth 2.1, tokens encrypted at rest, scoped per user."
    ),
    "github": (
        "Third-party, operated by GitHub. Trusted for repository ingestion because it "
        "is the official MCP server for a service the user already has an account "
        "with, and it is read-only in our configuration (repo contents only)."
    ),
}


def auth_for(server: str) -> str:
    return {
        "portfolio": "None (local, read-only)",
        "marketplace": "Bearer token to GigSim; OAuth 2.1 to live Upwork",
        "workspace": "None (mock); OAuth to Google when enabled",
    }.get(server, "-")


def annotation_summary(annotations: dict) -> str:
    if not annotations:
        return "-"
    parts = []
    if annotations.get("readOnlyHint"):
        parts.append("readOnly")
    if annotations.get("destructiveHint"):
        parts.append("**destructive**")
    if annotations.get("idempotentHint"):
        parts.append("idempotent")
    if annotations.get("openWorldHint"):
        parts.append("openWorld")
    return ", ".join(parts) or "-"


def first_line(text: str) -> str:
    line = (text or "").strip().split("\n")[0].strip()
    return line.replace("|", "\\|")


async def main() -> None:
    registry = MCPToolRegistry()
    rows: list[str] = []
    sections: list[str] = []
    total = 0

    for server in ("portfolio", "marketplace", "workspace"):
        tools = await registry.list_tools(server)
        total += len(tools)
        sections.append(f"\n### `{server}` ({len(tools)} tools)\n\n{SERVER_NOTES[server]}\n")
        sections.append("| Tool | Read/Write | Annotations | Auth | What it does |")
        sections.append("| --- | --- | --- | --- | --- |")
        for tool in sorted(tools, key=lambda t: t["name"]):
            annotations = tool["annotations"] or {}
            kind = "read" if annotations.get("readOnlyHint") else "write"
            sections.append(
                f"| `{tool['name']}` | {kind} | {annotation_summary(annotations)} | "
                f"{auth_for(server)} | {first_line(tool['description'])} |"
            )
            rows.append(f"{server}.{tool['name']}")

    allowlist = ["| Agent | Server | Tools it may call |", "| --- | --- | --- |"]
    for agent, servers in AGENT_TOOLS.items():
        if not servers:
            allowlist.append(f"| `{agent}` | — | **none** (pure reasoning; holds no tools) |")
            continue
        for srv, names in servers.items():
            allowlist.append(f"| `{agent}` | `{srv}` | {', '.join(f'`{n}`' for n in names)} |")

    content = f"""# Tool surface

Generated from the live MCP servers by `python -m scripts.generate_tools_doc`.
Do not edit by hand: CI regenerates this file and fails if it differs, so a
tool added without documentation breaks the build.

**{total} tools across 3 first-party servers**, plus two third-party servers
used through the same discipline.

Last generated: {datetime.now(UTC).isoformat(timespec='seconds')}

---

## First-party servers
{chr(10).join(sections)}

---

## Third-party servers

| Server | Why it is here | Trust rationale |
| --- | --- | --- |
| **Upwork official MCP** (`https://mcp.upwork.com/mcp`) | Live mode: job search, proposal drafts, messages. | {TRUST['upwork_official']} |
| **GitHub MCP** (`https://api.githubcopilot.com/mcp/`) | Ingests repository READMEs and code into the portfolio corpus. | {TRUST['github']} |

Trust rationale for the first-party servers:

| Server | Rationale |
| --- | --- |
| `portfolio` | {TRUST['portfolio']} |
| `marketplace` | {TRUST['marketplace']} |
| `workspace` | {TRUST['workspace']} |

---

## Per-agent tool allowlist

Each agent is handed only the tools it needs, filtered by name at the client.
This is a security boundary, not documentation: a node physically cannot call
what it was not given, and `tests/integration/test_graph_guarantees.py` proves
it by trying.

Note that the **Job Analyst holds no tools at all**. It is the node that reads
attacker-controlled text, and giving that node tool access is precisely the
combination that makes prompt injection dangerous.

{chr(10).join(allowlist)}

---

## Write-tool safety contract

Every write tool, on every server, satisfies all four of these. The contract
tests in `tests/contract/test_tool_schemas.py` enforce them.

| Property | Why | Enforced by |
| --- | --- | --- |
| `idempotency_key` is required | A retried call after a timeout must not submit twice or spend Connects twice. | `test_every_write_tool_takes_an_idempotency_key` |
| `dry_run` returns the exact payload and cost | The human approves the real call, not a summary of it. | `test_every_write_tool_supports_dry_run` |
| `approval_token` is required to execute | The agent cannot reach the marketplace on its own, whatever a job post tells it. | `test_every_write_tool_requires_an_approval_token` |
| Annotations are honest | `submit_proposal` spends Connects irreversibly and is marked `destructiveHint`. | `test_irreversible_tools_are_marked_destructive` |

## Schema snapshot

`tests/contract/tool_schema_snapshot.json` records every signature above. An
unintended change fails CI with a diff naming the tool. An intended change is
recorded with `make snapshot`, putting the new interface in the commit where a
reviewer sees it.
"""
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(content, encoding="utf-8")
    print(f"wrote {OUT} ({total} tools)")


if __name__ == "__main__":
    asyncio.run(main())
