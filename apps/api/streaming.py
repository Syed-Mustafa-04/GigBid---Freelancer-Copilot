"""The SSE bridge between LangGraph's stream and the browser.

Server-Sent Events rather than WebSockets: the traffic is one-directional
(the server reports progress, the browser does not talk back mid-run), SSE
survives proxies that mangle WebSocket upgrades, and it reconnects on its own.
A WebSocket would be a second protocol to operate for no capability we use.

Two details here are load-bearing and easy to get wrong:

* **Frame boundaries.** An SSE frame ends with a blank line. A chunk arriving
  split across TCP reads must not be parsed as two half-frames, which is why
  the client buffers and splits on ``\\n\\n`` rather than per chunk.
* **Disconnect handling.** If the user closes the tab mid-run the generator
  must stop, or the graph keeps executing against a socket nobody is reading.
  The run itself is checkpointed, so stopping the stream never loses work -
  the thread is picked up from the database on the next request.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

from fastapi import Request
from fastapi.responses import StreamingResponse

SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    # nginx buffers responses by default, which turns a live stream into one
    # delivery at the end. This is the header that stops it.
    "X-Accel-Buffering": "no",
}

HEARTBEAT_SECONDS = 15.0


def encode(payload: dict[str, Any]) -> str:
    """One SSE frame. The trailing blank line is the frame delimiter."""
    return f"data: {json.dumps(payload, default=str)}\n\n"


def comment(text: str = "keep-alive") -> str:
    """An SSE comment: keeps the connection open without delivering an event."""
    return f": {text}\n\n"


async def with_heartbeat(
    source: AsyncIterator[dict[str, Any]],
    *,
    interval: float = HEARTBEAT_SECONDS,
) -> AsyncIterator[str]:
    """Emit a comment frame when the source goes quiet.

    A node can take 30s. Proxies and load balancers close idle connections
    well before that, so a long node would look to the browser like a dropped
    run. The heartbeat is invisible to the client's event handler because
    comment frames carry no data.
    """
    queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

    async def pump() -> None:
        try:
            async for event in source:
                await queue.put(event)
        finally:
            await queue.put(None)

    task = asyncio.create_task(pump())
    try:
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=interval)
            except TimeoutError:
                yield comment()
                continue
            if event is None:
                return
            yield encode(event)
    finally:
        task.cancel()


def sse_response(
    source: AsyncIterator[dict[str, Any]],
    request: Request | None = None,
    *,
    heartbeat: bool = True,
) -> StreamingResponse:
    """Wrap an async event source as an SSE response.

    Errors are delivered as a final ``event: "error"`` frame rather than being
    allowed to become a truncated stream, because a stream that simply stops
    is indistinguishable in the browser from a network failure.
    """

    async def generator() -> AsyncIterator[str]:
        try:
            stream = with_heartbeat(source) if heartbeat else _plain(source)
            async for frame in stream:
                if request is not None and await request.is_disconnected():
                    break
                yield frame
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            from agent.safety import mask_pii

            yield encode(
                {
                    "event": "error",
                    "detail": mask_pii(f"{type(exc).__name__}: {exc}")[:300],
                }
            )

    return StreamingResponse(generator(), media_type="text/event-stream", headers=SSE_HEADERS)


async def _plain(source: AsyncIterator[dict[str, Any]]) -> AsyncIterator[str]:
    async for event in source:
        yield encode(event)
