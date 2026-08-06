"""Phase 2 DoD: a mock run produces a correct span tree *in Postgres* (asserted shape)."""

import os
import uuid
from decimal import Decimal

import asyncpg
from pydantic import BaseModel

from backline.core.guardrails import RunLimits
from backline.core.runtime import AgentRuntime, AgentSpec, Tool
from backline.core.trace import PostgresSink, Tracer
from backline.db.migrate import run_migrations
from backline.providers.base import ToolCall
from backline.providers.mock import MockProvider, MockTurn
from backline.providers.registry import ModelRegistry
from tests.conftest import requires_postgres


class EchoParams(BaseModel):
    value: str


async def echo(params: EchoParams) -> str:
    return f"echo: {params.value}"


@requires_postgres
async def test_mock_run_persists_run_row_and_span_tree() -> None:
    database_url = os.environ["DATABASE_URL"]
    await run_migrations(database_url)  # idempotent; app schema must exist

    provider = MockProvider(
        [
            MockTurn(tool_calls=[ToolCall(id="c1", name="echo", arguments={"value": "hi"})]),
            MockTurn(text="All done."),
        ]
    )
    sink = PostgresSink(database_url)
    runtime = AgentRuntime(
        providers={"mock": provider},
        registry=ModelRegistry.load(),
        tracer=Tracer([sink]),
    )
    agent = AgentSpec(
        name="pg-integration",
        system_prompt="Echo things.",
        model="mock-sonnet",
        tools=[Tool(name="echo", description="Echo a value.", params=EchoParams, handler=echo)],
        limits=RunLimits(max_iterations=4, run_budget_usd=Decimal("1")),
    )

    result = await runtime.run(agent, "Say hi.", session_id=None)
    await sink.aclose()

    conn = await asyncpg.connect(database_url)
    try:
        run_row = await conn.fetchrow("SELECT * FROM app.runs WHERE id = $1", result.run_id)
        assert run_row is not None
        assert run_row["agent"] == "pg-integration"
        assert run_row["status"] == "completed"
        assert run_row["finished_at"] is not None
        assert run_row["cost_usd"] == Decimal("0.001620")  # NUMERIC → Decimal, never float
        assert isinstance(run_row["cost_usd"], Decimal)

        span_rows = await conn.fetch(
            "SELECT * FROM app.spans WHERE run_id = $1 ORDER BY started_at, ended_at",
            result.run_id,
        )
        assert len(span_rows) == 5  # llm, tool, iter1, llm, iter2
        by_name = {row["name"]: row for row in span_rows}
        assert set(by_name) == {"iteration:1", "iteration:2", "llm:mock-sonnet", "tool:echo"}

        # Shape: iterations hang off the run (parent NULL); llm/tool hang off iterations.
        iter1, iter2 = by_name["iteration:1"], by_name["iteration:2"]
        assert iter1["parent_id"] is None and iter2["parent_id"] is None
        assert iter1["kind"] == "iteration"
        tool_span = by_name["tool:echo"]
        assert tool_span["parent_id"] == iter1["id"]
        assert tool_span["kind"] == "tool_call"
        llm_spans = [r for r in span_rows if r["kind"] == "llm_call"]
        assert len(llm_spans) == 2
        assert {r["parent_id"] for r in llm_spans} == {iter1["id"], iter2["id"]}
        for row in span_rows:
            assert row["ended_at"] is not None
            assert isinstance(row["id"], uuid.UUID)

        # attrs JSONB: cost serialized as a string by the one Decimal-safe encoder.
        llm_attrs = await conn.fetchval(
            "SELECT attrs->>'cost_usd' FROM app.spans "
            "WHERE run_id = $1 AND kind = 'llm_call' ORDER BY started_at LIMIT 1",
            result.run_id,
        )
        assert llm_attrs == "0.000810"
    finally:
        await conn.execute("DELETE FROM app.spans WHERE run_id = $1", result.run_id)
        await conn.execute("DELETE FROM app.runs WHERE id = $1", result.run_id)
        await conn.close()
