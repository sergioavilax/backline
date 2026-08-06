"""Server-sent-events plumbing (no extra dependency — the format is three lines).

BUILD_PLAN §9: SSE through the compose network needs proxy buffering disabled and
heartbeat comments every 15s; both live here so every stream behaves identically.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Coroutine
from typing import Any

from fastapi.responses import StreamingResponse

from backline.jsonutil import canonical_dumps

HEARTBEAT_S = 15.0

SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",  # nginx-style proxies: do not buffer this stream
}


def sse_event(event: str, data: Any) -> str:
    """One SSE frame. ``data`` serializes via the repo's one Decimal-safe encoder."""
    return f"event: {event}\ndata: {canonical_dumps(data)}\n\n"


def sse_comment(text: str = "keepalive") -> str:
    return f": {text}\n\n"


async def heartbeat_stream(
    queue: asyncio.Queue[str | None],
) -> AsyncIterator[str]:
    """Drain frames from ``queue`` (None = end of stream), interleaving heartbeat
    comments whenever ``HEARTBEAT_S`` passes without a frame."""
    while True:
        try:
            frame = await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_S)
        except TimeoutError:
            yield sse_comment()
            continue
        if frame is None:
            return
        yield frame


def sse_response(gen: AsyncIterator[str]) -> StreamingResponse:
    return StreamingResponse(gen, media_type="text/event-stream", headers=SSE_HEADERS)


async def pump(
    producer: Callable[[asyncio.Queue[str | None]], Coroutine[Any, Any, None]],
    queue: asyncio.Queue[str | None],
) -> None:
    """Run ``producer`` until done, then close the stream; producer errors surface
    as an ``error`` event rather than a torn connection."""
    try:
        await producer(queue)
    except Exception as exc:
        queue.put_nowait(sse_event("error", {"detail": f"{type(exc).__name__}: {exc}"}))
    finally:
        queue.put_nowait(None)
