"""Sessions + the chat message stream (§6 surface 1)."""

from __future__ import annotations

import asyncio
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from backline.api import chat
from backline.api.schemas import (
    MessageIn,
    MessageOut,
    SessionCreate,
    SessionDetailOut,
    SessionOut,
)
from backline.api.sse import heartbeat_stream, pump, sse_response
from backline.api.state import AppState, get_state, jload

router = APIRouter(tags=["sessions"])

State = Annotated[AppState, Depends(get_state)]

_SESSION_COLUMNS = """
    s.id, s.title, s.created_at,
    (SELECT count(*) FROM app.messages m WHERE m.session_id = s.id) AS n_messages,
    (SELECT max(m.created_at) FROM app.messages m WHERE m.session_id = s.id)
        AS last_message_at
"""


@router.post("/sessions", response_model=SessionOut, status_code=201)
async def create_session(body: SessionCreate, state: State) -> SessionOut:
    row = await state.pool.fetchrow(
        "INSERT INTO app.sessions (title) VALUES ($1) RETURNING id, title, created_at",
        body.title,
    )
    assert row is not None
    return SessionOut(id=row["id"], title=row["title"], created_at=row["created_at"])


@router.get("/sessions", response_model=list[SessionOut])
async def list_sessions(state: State, limit: int = 50, offset: int = 0) -> list[SessionOut]:
    rows = await state.pool.fetch(
        f"SELECT {_SESSION_COLUMNS} FROM app.sessions s "
        "ORDER BY COALESCE((SELECT max(m.created_at) FROM app.messages m "
        " WHERE m.session_id = s.id), s.created_at) DESC "
        "LIMIT $1 OFFSET $2",
        min(limit, 200),
        max(offset, 0),
    )
    return [SessionOut(**dict(row)) for row in rows]


@router.get("/sessions/{session_id}", response_model=SessionDetailOut)
async def get_session(session_id: uuid.UUID, state: State) -> SessionDetailOut:
    row = await state.pool.fetchrow(
        f"SELECT {_SESSION_COLUMNS} FROM app.sessions s WHERE s.id = $1", session_id
    )
    if row is None:
        raise HTTPException(status_code=404, detail=f"no session {session_id}")
    messages = await state.pool.fetch(
        "SELECT id, session_id, role, content, created_at FROM app.messages "
        "WHERE session_id = $1 ORDER BY created_at, id",
        session_id,
    )
    return SessionDetailOut(
        session=SessionOut(**dict(row)),
        messages=[MessageOut(**{**dict(m), "content": jload(m["content"])}) for m in messages],
    )


@router.post("/sessions/{session_id}/messages")
async def post_message(session_id: uuid.UUID, body: MessageIn, state: State) -> StreamingResponse:
    """Send a message; the reply is an SSE stream (see chat.py for the protocol).

    The turn itself runs in a background task — disconnecting only stops the
    stream, never the run.
    """
    exists = await state.pool.fetchval("SELECT 1 FROM app.sessions WHERE id = $1", session_id)
    if not exists:
        raise HTTPException(status_code=404, detail=f"no session {session_id}")

    queue: asyncio.Queue[str | None] = asyncio.Queue()

    async def producer(q: asyncio.Queue[str | None]) -> None:
        await chat.run_chat_turn(state, session_id, body.text, body.agent, q)

    task = asyncio.create_task(pump(producer, queue))
    state.track(task)
    return sse_response(heartbeat_stream(queue))
