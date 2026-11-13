"""MCP tool-schema contract tests (A7).

A tool signature is a published interface: other hosts connect to the Portfolio
server, and the graph's prompts describe arguments by name. Changing one
silently breaks callers in a way no unit test catches, because every component
still works in isolation.

So the full schema surface is snapshotted. An unintended change fails CI with a
diff. An intended change is recorded with ``make snapshot``, which puts the new
interface in the commit where a reviewer can see it.

Also enforced here, because they are contract properties rather than behaviour:

* every write tool is annotated honestly (``submit_proposal`` is destructive);
* every write tool takes an idempotency key and a ``dry_run``;
* read tools claim ``readOnlyHint`` and really take no mutating argument.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent.mcp_client import MCPToolRegistry

SNAPSHOT = Path(__file__).parent / "tool_schema_snapshot.json"

WRITE_TOOLS = {
    ("marketplace", "create_proposal_draft"),
    ("marketplace", "submit_proposal"),
    ("marketplace", "send_message"),
}
DESTRUCTIVE_TOOLS = {("marketplace", "submit_proposal")}


@pytest.fixture(scope="module")
async def snapshot_now() -> dict:
    return await MCPToolRegistry().schema_snapshot()


class TestSchemaSnapshot:
    async def test_matches_recorded_snapshot(self, snapshot_now):
        if not SNAPSHOT.exists():
            pytest.skip("no snapshot recorded yet; run `make snapshot`")
        recorded = json.loads(SNAPSHOT.read_text(encoding="utf-8"))

        differences: list[str] = []
        for server, tools in recorded.items():
            if server not in snapshot_now:
                differences.append(f"server '{server}' disappeared")
                continue
            for tool, schema in tools.items():
                live = snapshot_now[server].get(tool)
                if live is None:
                    differences.append(f"{server}.{tool} was removed")
                elif live != schema:
                    differences.append(f"{server}.{tool} signature changed")
        for server, tools in snapshot_now.items():
            for tool in tools:
                if tool not in recorded.get(server, {}):
                    differences.append(f"{server}.{tool} was added")

        assert not differences, (
            "MCP tool signatures changed:\n  - "
            + "\n  - ".join(differences)
            + "\n\nIf this was intended, run `make snapshot` and commit the new file."
        )


class TestAnnotationsAreHonest:
    async def test_write_tools_are_not_marked_read_only(self, snapshot_now):
        for server, tool in WRITE_TOOLS:
            annotations = snapshot_now.get(server, {}).get(tool, {}).get("annotations", {})
            assert annotations.get("readOnlyHint") is not True, (
                f"{server}.{tool} writes but claims readOnlyHint"
            )

    async def test_irreversible_tools_are_marked_destructive(self, snapshot_now):
        for server, tool in DESTRUCTIVE_TOOLS:
            annotations = snapshot_now.get(server, {}).get(tool, {}).get("annotations", {})
            assert annotations.get("destructiveHint") is True, (
                f"{server}.{tool} spends Connects irreversibly and must be destructiveHint=True"
            )

    async def test_read_tools_claim_read_only(self, snapshot_now):
        read_tools = [
            ("portfolio", "search_evidence"),
            ("portfolio", "get_case_study"),
            ("marketplace", "search_jobs"),
            ("marketplace", "get_client_history"),
            ("marketplace", "get_market_rates"),
        ]
        for server, tool in read_tools:
            entry = snapshot_now.get(server, {}).get(tool)
            if entry is None:
                continue
            assert entry["annotations"].get("readOnlyHint") is True, f"{server}.{tool}"


class TestWriteToolSafetyArguments:
    async def test_every_write_tool_takes_an_idempotency_key(self, snapshot_now):
        for server, tool in WRITE_TOOLS:
            schema = snapshot_now.get(server, {}).get(tool, {}).get("input_schema", {})
            properties = schema.get("properties", {})
            assert "idempotency_key" in properties, (
                f"{server}.{tool} has no idempotency_key: a retry could double-spend"
            )

    async def test_every_write_tool_supports_dry_run(self, snapshot_now):
        for server, tool in WRITE_TOOLS:
            properties = (
                snapshot_now.get(server, {}).get(tool, {}).get("input_schema", {}).get("properties", {})
            )
            assert "dry_run" in properties, (
                f"{server}.{tool} has no dry_run: the approval screen could not preview it"
            )

    async def test_every_write_tool_requires_an_approval_token(self, snapshot_now):
        for server, tool in WRITE_TOOLS:
            properties = (
                snapshot_now.get(server, {}).get(tool, {}).get("input_schema", {}).get("properties", {})
            )
            assert "approval_token" in properties, (
                f"{server}.{tool} has no approval_token: nothing structurally stops an agent "
                "reaching the marketplace on its own"
            )


class TestToolDescriptionsAreUsable:
    async def test_every_tool_has_a_description(self, snapshot_now):
        registry = MCPToolRegistry()
        for server in ("portfolio", "marketplace", "workspace"):
            for tool in await registry.list_tools(server):
                assert tool["description"], f"{server}.{tool['name']} has no description"
                assert len(tool["description"]) > 40, (
                    f"{server}.{tool['name']} description is too short to guide a model"
                )
