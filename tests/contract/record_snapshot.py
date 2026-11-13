"""Record the MCP tool-schema snapshot.

Run `make snapshot` after an INTENTIONAL interface change, then commit the
result so the new signature appears in the diff a reviewer reads.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from agent.mcp_client import MCPToolRegistry

SNAPSHOT = Path(__file__).parent / "tool_schema_snapshot.json"


async def main() -> None:
    snapshot = await MCPToolRegistry().schema_snapshot()
    SNAPSHOT.write_text(json.dumps(snapshot, indent=2, sort_keys=True), encoding="utf-8")
    total = sum(len(tools) for tools in snapshot.values())
    print(f"recorded {total} tool signatures across {len(snapshot)} servers -> {SNAPSHOT}")
    for server, tools in sorted(snapshot.items()):
        print(f"  {server}: {', '.join(sorted(tools))}")


if __name__ == "__main__":
    asyncio.run(main())
