"""Tracer unit tests: span tree shape, sinks, pubsub ordering — no Postgres needed."""

import json
import uuid
from decimal import Decimal
from pathlib import Path

import pytest

from backline.core.trace import InMemorySink, JsonlSink, TracePubSub, Tracer


async def test_span_tree_shape_and_pubsub_ordering(tmp_path: Path) -> None:
    memory = InMemorySink()
    pubsub = TracePubSub()
    tracer = Tracer([memory, JsonlSink(tmp_path), pubsub])

    run_id = uuid.uuid4()
    queue = pubsub.subscribe(run_id)

    async with tracer.run(agent="demo", run_id=run_id, meta={"model": "mock-sonnet"}) as run:
        async with run.span("iteration", "iteration:1") as iteration:
            async with iteration.span("llm_call", "llm:mock-sonnet") as llm:
                llm.attrs["gen_ai.usage.input_tokens"] = 120
                llm.attrs["cost_usd"] = Decimal("0.000810")
            async with iteration.span("tool_call", "tool:lookup") as tool:
                tool.attrs["tool"] = "lookup"
        run.set_result(status="completed", cost_usd=Decimal("0.000810"))

    # ── in-memory: completed span records with correct parentage ──
    spans = {s.name: s for s in memory.spans}
    assert set(spans) == {"iteration:1", "llm:mock-sonnet", "tool:lookup"}
    iteration_span = spans["iteration:1"]
    assert iteration_span.kind == "iteration"
    assert iteration_span.parent_id is None  # direct child of the run
    assert spans["llm:mock-sonnet"].parent_id == iteration_span.id
    assert spans["tool:lookup"].parent_id == iteration_span.id
    for s in memory.spans:
        assert s.run_id == run_id
        assert s.ended_at is not None
        assert s.ended_at >= s.started_at
    assert spans["llm:mock-sonnet"].attrs["status"] == "ok"

    run_record = memory.runs[-1]
    assert run_record.id == run_id
    assert run_record.status == "completed"
    assert run_record.cost_usd == Decimal("0.000810")
    assert run_record.finished_at is not None

    # ── pubsub: live event ordering for the SSE panel ──
    events = []
    while not queue.empty():
        events.append(queue.get_nowait())
    assert [(e.type, e.name) for e in events] == [
        ("run_start", "demo"),
        ("span_start", "iteration:1"),
        ("span_start", "llm:mock-sonnet"),
        ("span_end", "llm:mock-sonnet"),
        ("span_start", "tool:lookup"),
        ("span_end", "tool:lookup"),
        ("span_end", "iteration:1"),
        ("run_end", "demo"),
    ]

    # ── JSONL: one file per run, Decimals as strings ──
    jsonl = tmp_path / f"{run_id}.jsonl"
    lines = [json.loads(line) for line in jsonl.read_text(encoding="utf-8").splitlines()]
    assert lines[0]["type"] == "run_start"
    assert lines[-1]["type"] == "run_end"
    assert lines[-1]["run"]["cost_usd"] == "0.000810"  # Decimal → string, never a float
    span_lines = [ln for ln in lines if ln["type"] == "span_end"]
    assert {ln["span"]["kind"] for ln in span_lines} == {"iteration", "llm_call", "tool_call"}
    llm_line = next(ln for ln in span_lines if ln["span"]["kind"] == "llm_call")
    assert llm_line["span"]["attrs"]["cost_usd"] == "0.000810"


async def test_exception_marks_span_and_run_as_error() -> None:
    memory = InMemorySink()
    tracer = Tracer([memory])

    with pytest.raises(RuntimeError, match="boom"):
        async with tracer.run(agent="demo") as run:
            async with run.span("iteration", "iteration:1"):
                async with run.span("llm_call", "llm:x"):
                    raise RuntimeError("boom")

    assert memory.runs[-1].status == "error"
    assert "boom" in str(memory.runs[-1].meta.get("error"))
    by_name = {s.name: s for s in memory.spans}
    assert by_name["llm:x"].attrs["status"] == "error"
    assert "boom" in by_name["llm:x"].attrs["error"]
    assert by_name["iteration:1"].attrs["status"] == "error"


async def test_exhausted_status_set_by_caller_survives() -> None:
    memory = InMemorySink()
    tracer = Tracer([memory])
    async with tracer.run(agent="demo") as run:
        run.set_result(status="exhausted", cost_usd=Decimal("0.60"))
    assert memory.runs[-1].status == "exhausted"
    assert memory.runs[-1].cost_usd == Decimal("0.60")


async def test_pubsub_unsubscribe_and_isolation() -> None:
    pubsub = TracePubSub()
    tracer = Tracer([pubsub])
    watched, other = uuid.uuid4(), uuid.uuid4()
    queue = pubsub.subscribe(watched)

    async with tracer.run(agent="a", run_id=other):
        pass
    assert queue.empty()  # events for other runs never arrive

    pubsub.unsubscribe(watched, queue)
    async with tracer.run(agent="a", run_id=watched):
        pass
    assert queue.empty()  # unsubscribed


async def test_run_ids_are_unique_and_returned() -> None:
    memory = InMemorySink()
    tracer = Tracer([memory])
    async with tracer.run(agent="one") as first:
        pass
    async with tracer.run(agent="two") as second:
        pass
    assert first.run_id != second.run_id
    assert {r.agent for r in memory.runs} == {"one", "two"}
