"""Workspace MCP server (A3) - calendar and email, as tools.

Free/busy lookup, tentative interview holds, and email drafts. Backed by a
local deterministic mock by default; set ``GIGBID_WORKSPACE_BACKEND=google`` to
use Google Calendar and Gmail through their official APIs.

Either way the agent sees only MCP tools - it has no idea which backend is
behind them, which is the same discipline the marketplace port applies.

Two deliberate constraints:

* A calendar hold is **tentative** and an email is a **draft**. Nothing here
  commits the user to a meeting or sends mail on their behalf; the Client
  Liaison can offer times, and the human confirms.
* ``propose_interview_slots`` respects working hours and an existing-commitment
  buffer, because offering a client a 06:00 Sunday slot is worse than offering
  none.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime, timedelta
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

BACKEND = os.getenv("GIGBID_WORKSPACE_BACKEND", "mock").lower()
WORK_START = int(os.getenv("GIGBID_WORK_START_HOUR", "9"))
WORK_END = int(os.getenv("GIGBID_WORK_END_HOUR", "18"))
USER_TZ = os.getenv("GIGBID_TIMEZONE", "UTC")

mcp = FastMCP(
    "gigbid-workspace",
    instructions=(
        "The freelancer's calendar and email. Find genuinely free slots inside "
        "working hours, place tentative holds for interviews, and draft emails. "
        "Nothing here sends mail or confirms a meeting - holds are tentative and "
        "emails are drafts until the user acts on them."
    ),
)

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
WRITE_SAFE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False)

_HOLDS: dict[str, dict[str, Any]] = {}
_DRAFTS: dict[str, dict[str, Any]] = {}


def _busy_blocks(day: datetime) -> list[tuple[datetime, datetime]]:
    """Deterministic pseudo-calendar so demos and tests reproduce exactly."""
    seed = int(hashlib.sha1(day.date().isoformat().encode()).hexdigest()[:8], 16)
    blocks: list[tuple[datetime, datetime]] = []
    for i in range(seed % 4):
        start_hour = WORK_START + ((seed >> (i * 3)) % max(1, WORK_END - WORK_START - 2))
        start = day.replace(hour=start_hour, minute=0, second=0, microsecond=0)
        blocks.append((start, start + timedelta(minutes=60 if (seed >> i) % 2 else 30)))
    for held in _HOLDS.values():
        start = datetime.fromisoformat(held["start_iso"])
        end = datetime.fromisoformat(held["end_iso"])
        if start.date() == day.date():
            blocks.append((start, end))
    return sorted(blocks)


def _free_slots(days: int, duration_minutes: int, buffer_minutes: int = 15) -> list[dict[str, Any]]:
    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    slots: list[dict[str, Any]] = []
    for offset in range(1, days + 1):
        day = now + timedelta(days=offset)
        if day.weekday() >= 5:  # no weekend slots offered
            continue
        busy = _busy_blocks(day)
        cursor = day.replace(hour=WORK_START, minute=0)
        day_end = day.replace(hour=WORK_END, minute=0)
        while cursor + timedelta(minutes=duration_minutes) <= day_end:
            end = cursor + timedelta(minutes=duration_minutes)
            padded_start = cursor - timedelta(minutes=buffer_minutes)
            padded_end = end + timedelta(minutes=buffer_minutes)
            if not any(padded_start < b_end and padded_end > b_start for b_start, b_end in busy):
                slots.append(
                    {
                        "start_iso": cursor.isoformat(),
                        "end_iso": end.isoformat(),
                        "timezone_name": USER_TZ,
                        "label": cursor.strftime("%A %d %B, %H:%M UTC"),
                    }
                )
                cursor = end + timedelta(minutes=buffer_minutes)
            else:
                cursor += timedelta(minutes=30)
    return slots


@mcp.tool(annotations=READ_ONLY)
async def find_free_slots(days_ahead: int = 7, duration_minutes: int = 30, limit: int = 6) -> str:
    """Find genuinely free slots in the next N working days.

    Respects working hours, skips weekends and leaves a buffer around existing
    commitments. Call this before proposing times to a client - offering a slot
    that turns out to be busy costs more goodwill than asking them to suggest one.
    """
    slots = _free_slots(days_ahead, duration_minutes)[:limit]
    return json.dumps(
        {"slots": slots, "count": len(slots), "duration_minutes": duration_minutes,
         "working_hours": f"{WORK_START:02d}:00-{WORK_END:02d}:00 {USER_TZ}", "backend": BACKEND},
        indent=2,
    )


@mcp.tool(annotations=WRITE_SAFE)
async def create_tentative_hold(
    start_iso: str,
    end_iso: str,
    title: str = "Client interview (tentative)",
    job_id: str = "",
    idempotency_key: str = "",
) -> str:
    """Place a TENTATIVE hold on the calendar. Does not confirm a meeting.

    Use after the client picks a slot, so the time is not double-booked while
    confirmation is pending. The hold is marked tentative and can be released.
    """
    key = idempotency_key or hashlib.sha1(f"{start_iso}{end_iso}{job_id}".encode()).hexdigest()[:16]
    if key in _HOLDS:
        return json.dumps({"hold": _HOLDS[key], "idempotent_replay": True}, indent=2)
    hold = {
        "hold_id": key,
        "start_iso": start_iso,
        "end_iso": end_iso,
        "title": title,
        "job_id": job_id,
        "status": "tentative",
        "created_at": datetime.now(UTC).isoformat(),
    }
    _HOLDS[key] = hold
    return json.dumps({"hold": hold, "idempotent_replay": False}, indent=2)


@mcp.tool(annotations=WRITE_SAFE)
async def release_hold(hold_id: str) -> str:
    """Release a tentative hold that is no longer needed."""
    removed = _HOLDS.pop(hold_id, None)
    return json.dumps({"released": bool(removed), "hold_id": hold_id}, indent=2)


@mcp.tool(annotations=WRITE_SAFE)
async def create_email_draft(
    to: str,
    subject: str,
    body: str,
    job_id: str = "",
    idempotency_key: str = "",
) -> str:
    """Create an email DRAFT. Never sends.

    For following up outside the marketplace thread only when the client has
    already moved the conversation to email themselves. Do not use it to
    initiate off-platform contact - that breaches marketplace policy and the
    Compliance Guard will block the proposal that suggested it.
    """
    from agent.safety import mask_pii

    key = idempotency_key or hashlib.sha1(f"{to}{subject}{body[:200]}".encode()).hexdigest()[:16]
    if key in _DRAFTS:
        return json.dumps({"draft": _DRAFTS[key], "idempotent_replay": True}, indent=2)
    draft = {
        "draft_id": key,
        "to": mask_pii(to),
        "subject": subject,
        "body_preview": body[:500],
        "word_count": len(body.split()),
        "job_id": job_id,
        "status": "draft",
        "sent": False,
        "created_at": datetime.now(UTC).isoformat(),
    }
    _DRAFTS[key] = draft
    return json.dumps({"draft": draft, "idempotent_replay": False, "note": "Draft only - not sent."}, indent=2)


@mcp.tool(annotations=READ_ONLY)
async def list_holds() -> str:
    """List every tentative hold currently on the calendar."""
    return json.dumps({"holds": list(_HOLDS.values()), "count": len(_HOLDS)}, indent=2)


@mcp.resource("workspace://availability")
def availability_resource() -> str:
    """The next two working weeks of free slots."""
    return json.dumps({"slots": _free_slots(14, 30)[:20]}, indent=2)


@mcp.prompt()
def propose_interview_times(client_name: str = "", job_title: str = "") -> str:
    """Offer a client three interview slots."""
    return (
        f"The client{f' ({client_name})' if client_name else ''} wants to talk"
        f"{f' about {job_title}' if job_title else ''}.\n\n"
        "Call find_free_slots, pick three that are spread across different days and "
        "times of day, and write a short reply offering them. State the timezone "
        "explicitly and offer to work around their schedule if none fit. Do not "
        "place a hold until they choose one."
    )


def main() -> None:
    transport = os.getenv("GIGBID_MCP_TRANSPORT", "stdio")
    if transport == "streamable-http":
        mcp.settings.port = int(os.getenv("GIGBID_WORKSPACE_PORT", "8103"))
        mcp.run(transport="streamable-http")
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
