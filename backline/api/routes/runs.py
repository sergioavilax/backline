"""Runs + spans: the Trace Inspector's data (§6 surface 2 — the signature).

The live stream merges two sources (D-025):

- **in-proc pubsub** — runs executing inside this API process stream span events the
  instant they happen (the amber pulse is real time, not polling);
- **DB polling fallback** — runs driven by another process (CLI harness, eval
  runner) only have their Postgres rows; while such a run is unfinished the stream
  re-reads ``app.spans`` every ``_POLL_S`` and emits what changed.

Client contract: ``snapshot`` first (run + all spans so far), then ``span_start`` /
``span_end`` / ``run_end`` upserts. Events can arrive more than once (subscribe races
the snapshot read) — clients upsert by span id and never downgrade an ended span to
running. The stream closes after ``run_end``.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from typing import Annotated, Any

import asyncpg
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from backline.api.schemas import RunDetailOut, RunListOut, RunOut, SpanOut
from backline.api.sse import HEARTBEAT_S, sse_comment, sse_event, sse_response
from backline.api.state import AppState, get_state, jload
from backline.core.trace import TraceEvent

router = APIRouter(tags=["runs"])

State = Annotated[AppState, Depends(get_state)]

_POLL_S = 2.0
_TERMINAL = ("completed", "exhausted", "error")


def _run_out(row: asyncpg.Record) -> RunOut:
    return RunOut(**{**dict(row), "meta": jload(row["meta"])})


def _span_out(row: asyncpg.Record) -> SpanOut:
    return SpanOut(**{**dict(row), "attrs": jload(row["attrs"])})


async def _fetch_run(pool: asyncpg.Pool, run_id: uuid.UUID) -> asyncpg.Record:
    row = await pool.fetchrow(
        "SELECT id, session_id, agent, status, started_at, finished_at, cost_usd, meta "
        "FROM app.runs WHERE id = $1",
        run_id,
    )
    if row is None:
        raise HTTPException(status_code=404, detail=f"no run {run_id}")
    return row


async def _fetch_spans(pool: asyncpg.Pool, run_id: uuid.UUID) -> list[asyncpg.Record]:
    rows: list[asyncpg.Record] = await pool.fetch(
        "SELECT id, run_id, parent_id, kind, name, started_at, ended_at, attrs "
        "FROM app.spans WHERE run_id = $1 ORDER BY started_at, id",
        run_id,
    )
    return rows


@router.get("/runs", response_model=RunListOut)
async def list_runs(
    state: State,
    session_id: uuid.UUID | None = None,
    agent: str | None = None,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> RunListOut:
    conditions = ["TRUE"]
    args: list[Any] = []
    if session_id is not None:
        args.append(session_id)
        conditions.append(f"session_id = ${len(args)}")
    if agent is not None:
        args.append(agent)
        conditions.append(f"agent = ${len(args)}")
    if status is not None:
        args.append(status)
        conditions.append(f"status = ${len(args)}")
    where = " AND ".join(conditions)
    total = await state.pool.fetchval(f"SELECT count(*) FROM app.runs WHERE {where}", *args)
    args.extend((min(limit, 200), max(offset, 0)))
    rows = await state.pool.fetch(
        "SELECT id, session_id, agent, status, started_at, finished_at, cost_usd, meta "
        f"FROM app.runs WHERE {where} ORDER BY started_at DESC "
        f"LIMIT ${len(args) - 1} OFFSET ${len(args)}",
        *args,
    )
    return RunListOut(runs=[_run_out(r) for r in rows], total=total)


@router.get("/runs/{run_id}", response_model=RunDetailOut)
async def get_run(run_id: uuid.UUID, state: State) -> RunDetailOut:
    run = await _fetch_run(state.pool, run_id)
    spans = await _fetch_spans(state.pool, run_id)
    return RunDetailOut(run=_run_out(run), spans=[_span_out(s) for s in spans])


@router.get("/runs/{run_id}/spans", response_model=list[SpanOut])
async def get_spans(run_id: uuid.UUID, state: State) -> list[SpanOut]:
    await _fetch_run(state.pool, run_id)
    return [_span_out(s) for s in await _fetch_spans(state.pool, run_id)]


def _live_frame(event: TraceEvent) -> str | None:
    if event.type in ("span_start", "span_end") and event.span is not None:
        payload = event.span.model_dump(mode="json")
        return sse_event(event.type, payload)
    if event.type == "run_end" and event.run is not None:
        return sse_event("run_end", event.run.model_dump(mode="json"))
    return None


@router.get("/runs/{run_id}/spans/stream")
async def stream_spans(run_id: uuid.UUID, state: State) -> StreamingResponse:
    """Replay-then-live span feed for one run (SSE). Closes after ``run_end``.

    Runs inline in the response generator (unlike chat): the feed is read-only, so
    a client disconnect should cancel the merge loop, not outlive it.
    """
    await _fetch_run(state.pool, run_id)

    async def stream() -> AsyncIterator[str]:
        # Subscribe before the snapshot read: no event can fall into the gap.
        live = state.pubsub.subscribe(run_id)
        ticks_since_beat = 0
        try:
            run_row = await _fetch_run(state.pool, run_id)
            span_rows = await _fetch_spans(state.pool, run_id)
            yield sse_event(
                "snapshot",
                {
                    "run": _run_out(run_row).model_dump(mode="json"),
                    "spans": [_span_out(s).model_dump(mode="json") for s in span_rows],
                },
            )
            if run_row["status"] in _TERMINAL:
                yield sse_event("run_end", _run_out(run_row).model_dump(mode="json"))
                return
            seen_ended: set[uuid.UUID] = {s["id"] for s in span_rows if s["ended_at"]}
            while True:
                try:
                    event = await asyncio.wait_for(live.get(), timeout=_POLL_S)
                except TimeoutError:
                    # Poll fallback: the run may be driven by another process whose
                    # spans only exist as Postgres rows (D-025).
                    ticks_since_beat += 1
                    if ticks_since_beat * _POLL_S >= HEARTBEAT_S:
                        ticks_since_beat = 0
                        yield sse_comment()
                    run_row = await _fetch_run(state.pool, run_id)
                    for span in await _fetch_spans(state.pool, run_id):
                        if span["ended_at"] and span["id"] not in seen_ended:
                            seen_ended.add(span["id"])
                            yield sse_event("span_end", _span_out(span).model_dump(mode="json"))
                    if run_row["status"] in _TERMINAL:
                        yield sse_event("run_end", _run_out(run_row).model_dump(mode="json"))
                        return
                    continue
                ticks_since_beat = 0
                frame = _live_frame(event)
                if frame is not None:
                    if event.type == "span_end" and event.span is not None:
                        seen_ended.add(event.span.id)
                    yield frame
                if event.type == "run_end":
                    return
        finally:
            state.pubsub.unsubscribe(run_id, live)

    return sse_response(stream())
