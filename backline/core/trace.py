"""Tracing (BUILD_PLAN §4.7): every run emits a span tree — nothing runs silently.

Span tree: ``run → iteration → {llm_call | tool_call | guardrail | compression}`` with
token/cost/latency attrs. Attribute names are OpenTelemetry-shaped (``gen_ai.*``) so an
OTel exporter would be a sink away — without dragging in a collector.

Sinks (fan-out, all fed the same event stream):

- ``PostgresSink``   — durable rows in ``app.runs`` / ``app.spans``
- ``JsonlSink``      — one ``{run_id}.jsonl`` per run under ``data/traces/``
- ``TracePubSub``    — in-proc queues per run, feeding the SSE panel in Phase 6
- ``InMemorySink``   — assertions in tests

``span_start`` events exist for the live view (pubsub); durable sinks persist completed
spans (``span_end``) plus the run start/finish, so one write per span.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, Protocol

import asyncpg
from pydantic import BaseModel, ConfigDict, Field

from backline.config import get_settings
from backline.jsonutil import canonical_dumps

RunStatus = Literal["running", "completed", "exhausted", "error"]
SpanKind = Literal["iteration", "llm_call", "tool_call", "guardrail", "compression"]


def _now() -> datetime:
    return datetime.now(UTC)


class RunRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: uuid.UUID
    session_id: uuid.UUID | None = None
    agent: str
    status: RunStatus
    started_at: datetime
    finished_at: datetime | None = None
    cost_usd: Decimal = Decimal("0")
    meta: dict[str, Any] = Field(default_factory=dict)


class SpanRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: uuid.UUID
    run_id: uuid.UUID
    parent_id: uuid.UUID | None = None
    kind: SpanKind
    name: str
    started_at: datetime
    ended_at: datetime | None = None  # None on span_start events only
    attrs: dict[str, Any] = Field(default_factory=dict)


class TraceEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    type: Literal["run_start", "span_start", "span_end", "run_end"]
    run: RunRecord | None = None
    span: SpanRecord | None = None

    @property
    def run_id(self) -> uuid.UUID:
        if self.run is not None:
            return self.run.id
        assert self.span is not None
        return self.span.run_id

    @property
    def name(self) -> str:
        if self.run is not None:
            return self.run.agent
        assert self.span is not None
        return self.span.name


class TraceSink(Protocol):
    async def emit(self, event: TraceEvent) -> None: ...


class InMemorySink:
    def __init__(self) -> None:
        self.events: list[TraceEvent] = []

    async def emit(self, event: TraceEvent) -> None:
        self.events.append(event)

    @property
    def spans(self) -> list[SpanRecord]:
        return [e.span for e in self.events if e.type == "span_end" and e.span is not None]

    @property
    def runs(self) -> list[RunRecord]:
        return [e.run for e in self.events if e.type == "run_end" and e.run is not None]


class JsonlSink:
    """One append-only ``{run_id}.jsonl`` per run; durable records only."""

    def __init__(self, directory: Path) -> None:
        self._dir = directory
        self._dir.mkdir(parents=True, exist_ok=True)

    async def emit(self, event: TraceEvent) -> None:
        if event.type == "span_start":
            return
        path = self._dir / f"{event.run_id}.jsonl"
        line = canonical_dumps(event.model_dump(mode="python", exclude_none=True))
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")


class TracePubSub:
    """In-proc fan-out of live trace events, keyed by run id (SSE feed in Phase 6)."""

    def __init__(self) -> None:
        self._subscribers: dict[uuid.UUID, list[asyncio.Queue[TraceEvent]]] = {}

    def subscribe(self, run_id: uuid.UUID) -> asyncio.Queue[TraceEvent]:
        queue: asyncio.Queue[TraceEvent] = asyncio.Queue()
        self._subscribers.setdefault(run_id, []).append(queue)
        return queue

    def unsubscribe(self, run_id: uuid.UUID, queue: asyncio.Queue[TraceEvent]) -> None:
        queues = self._subscribers.get(run_id, [])
        if queue in queues:
            queues.remove(queue)
        if not queues:
            self._subscribers.pop(run_id, None)

    async def emit(self, event: TraceEvent) -> None:
        for queue in self._subscribers.get(event.run_id, []):
            queue.put_nowait(event)


class PostgresSink:
    """Durable trace rows in ``app.runs`` / ``app.spans``.

    Spans insert on ``span_start`` (``ended_at`` NULL — in-flight spans are queryable
    mid-run) and complete on ``span_end``. Insert-on-start is also what satisfies the
    ``spans.parent_id`` self-FK: children *end* before their parents, but always
    *start* after them.
    """

    def __init__(self, database_url: str | None = None) -> None:
        self._url = database_url or get_settings().database_url
        self._conn: asyncpg.Connection | None = None
        self._lock = asyncio.Lock()

    async def _connection(self) -> asyncpg.Connection:
        if self._conn is None or self._conn.is_closed():
            self._conn = await asyncpg.connect(self._url)
        return self._conn

    async def emit(self, event: TraceEvent) -> None:
        async with self._lock:
            conn = await self._connection()
            if event.type == "run_start" and event.run is not None:
                await conn.execute(
                    "INSERT INTO app.runs (id, session_id, agent, status, started_at, meta) "
                    "VALUES ($1, $2, $3, $4, $5, $6::jsonb)",
                    event.run.id,
                    event.run.session_id,
                    event.run.agent,
                    event.run.status,
                    event.run.started_at,
                    canonical_dumps(event.run.meta),
                )
            elif event.type == "run_end" and event.run is not None:
                await conn.execute(
                    "UPDATE app.runs SET status = $2, finished_at = $3, cost_usd = $4, "
                    "meta = $5::jsonb WHERE id = $1",
                    event.run.id,
                    event.run.status,
                    event.run.finished_at,
                    event.run.cost_usd,
                    canonical_dumps(event.run.meta),
                )
            elif event.type == "span_start" and event.span is not None:
                await conn.execute(
                    "INSERT INTO app.spans "
                    "(id, run_id, parent_id, kind, name, started_at, attrs) "
                    "VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb)",
                    event.span.id,
                    event.span.run_id,
                    event.span.parent_id,
                    event.span.kind,
                    event.span.name,
                    event.span.started_at,
                    canonical_dumps(event.span.attrs),
                )
            elif event.type == "span_end" and event.span is not None:
                await conn.execute(
                    "UPDATE app.spans SET ended_at = $2, attrs = $3::jsonb WHERE id = $1",
                    event.span.id,
                    event.span.ended_at,
                    canonical_dumps(event.span.attrs),
                )

    async def aclose(self) -> None:
        if self._conn is not None and not self._conn.is_closed():
            await self._conn.close()


class SpanHandle:
    def __init__(
        self,
        emit: "Tracer",
        run_id: uuid.UUID,
        parent_id: uuid.UUID | None,
        kind: SpanKind,
        name: str,
    ) -> None:
        self._tracer = emit
        self.id = uuid.uuid4()
        self.run_id = run_id
        self.parent_id = parent_id
        self.kind: SpanKind = kind
        self.name = name
        self.started_at = _now()
        self.attrs: dict[str, Any] = {}

    @asynccontextmanager
    async def span(self, kind: SpanKind, name: str) -> "AsyncIterator[SpanHandle]":
        """Open a child span of this span."""
        async with self._tracer._span(self.run_id, self.id, kind, name) as child:
            yield child

    def _record(self, ended_at: datetime | None) -> SpanRecord:
        return SpanRecord(
            id=self.id,
            run_id=self.run_id,
            parent_id=self.parent_id,
            kind=self.kind,
            name=self.name,
            started_at=self.started_at,
            ended_at=ended_at,
            attrs=dict(self.attrs),
        )


class RunHandle:
    def __init__(
        self,
        tracer: "Tracer",
        run_id: uuid.UUID,
        agent: str,
        session_id: uuid.UUID | None,
        meta: dict[str, Any],
    ) -> None:
        self._tracer = tracer
        self.run_id = run_id
        self.agent = agent
        self.session_id = session_id
        self.meta = meta
        self.started_at = _now()
        self.status: RunStatus = "completed"
        self.cost_usd = Decimal("0")

    def set_result(self, *, status: RunStatus, cost_usd: Decimal) -> None:
        self.status = status
        self.cost_usd = cost_usd

    @asynccontextmanager
    async def span(self, kind: SpanKind, name: str) -> AsyncIterator[SpanHandle]:
        async with self._tracer._span(self.run_id, None, kind, name) as handle:
            yield handle

    def _record(self, *, status: RunStatus, finished: bool) -> RunRecord:
        return RunRecord(
            id=self.run_id,
            session_id=self.session_id,
            agent=self.agent,
            status=status,
            started_at=self.started_at,
            finished_at=_now() if finished else None,
            cost_usd=self.cost_usd,
            meta=dict(self.meta),
        )


class Tracer:
    def __init__(self, sinks: Sequence[TraceSink]) -> None:
        self._sinks = list(sinks)

    async def _emit(self, event: TraceEvent) -> None:
        for sink in self._sinks:
            await sink.emit(event)

    @asynccontextmanager
    async def run(
        self,
        *,
        agent: str,
        run_id: uuid.UUID | None = None,
        session_id: uuid.UUID | None = None,
        meta: dict[str, Any] | None = None,
    ) -> AsyncIterator[RunHandle]:
        handle = RunHandle(self, run_id or uuid.uuid4(), agent, session_id, dict(meta or {}))
        await self._emit(
            TraceEvent(type="run_start", run=handle._record(status="running", finished=False))
        )
        try:
            yield handle
        except BaseException as exc:
            handle.status = "error"
            handle.meta.setdefault("error", f"{type(exc).__name__}: {exc}")
            raise
        finally:
            await self._emit(
                TraceEvent(type="run_end", run=handle._record(status=handle.status, finished=True))
            )

    @asynccontextmanager
    async def _span(
        self,
        run_id: uuid.UUID,
        parent_id: uuid.UUID | None,
        kind: SpanKind,
        name: str,
    ) -> AsyncIterator[SpanHandle]:
        handle = SpanHandle(self, run_id, parent_id, kind, name)
        await self._emit(TraceEvent(type="span_start", span=handle._record(ended_at=None)))
        try:
            yield handle
        except BaseException as exc:
            handle.attrs["status"] = "error"
            handle.attrs.setdefault("error", f"{type(exc).__name__}: {exc}")
            raise
        finally:
            handle.attrs.setdefault("status", "ok")
            await self._emit(TraceEvent(type="span_end", span=handle._record(ended_at=_now())))
