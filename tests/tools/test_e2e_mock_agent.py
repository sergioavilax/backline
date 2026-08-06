"""Phase 3 DoD: one mock-agent run exercising every tool (skips without DATABASE_URL).

A scripted MockProvider drives the real ``AgentRuntime`` through all nine §4.3 tools
against the seeded world — including one adversarial ``sql_query`` against ``truth``
that must die at the guardrail as a traced incident while the run itself recovers and
completes. Asserts the span tree, the staging writes, and the run-id stamping that
flows through the ambient run context.
"""

import os
import subprocess
import sys
from decimal import Decimal

import asyncpg
import pytest

from backline.config import get_settings
from backline.core.guardrails import RunLimits
from backline.core.runtime import AgentRuntime, AgentSpec
from backline.core.trace import InMemorySink, PostgresSink, Tracer
from backline.providers.base import ToolCall
from backline.providers.mock import MockProvider, MockTurn
from backline.providers.registry import ModelRegistry
from backline.rag.embed import run_embed
from backline.rag.embedder import HashingEmbedder
from backline.rag.reranker import LexicalReranker
from backline.tools import ToolContext, build_all_tools, sql_policy_check
from tests.conftest import REPO_ROOT, WorldEnv, requires_postgres

pytestmark = requires_postgres

EMIT_PERIOD = "2026-07"


@pytest.fixture(scope="module")
def emitted(world_env: WorldEnv) -> str:
    result = subprocess.run(
        [sys.executable, "-m", "datagen", "emit-period", EMIT_PERIOD],
        cwd=REPO_ROOT,
        env={**os.environ, "DATA_DIR": str(world_env.data_dir)},
        capture_output=True,
        text=True,
        check=False,
        timeout=600,
    )
    assert result.returncode == 0, result.stderr
    return EMIT_PERIOD


async def test_mock_agent_exercises_every_tool(
    emitted: str, world_env: WorldEnv, pool: asyncpg.Pool
) -> None:
    await run_embed(pool, data_dir=world_env.data_dir, embedder=HashingEmbedder())

    artist = await pool.fetchrow("SELECT id, stage_name FROM label.artists ORDER BY id LIMIT 1")
    contract_id = await pool.fetchval(
        "SELECT contract_id FROM rag.contract_chunks WHERE clause_no = '§3' "
        "ORDER BY contract_id LIMIT 1"
    )
    statement_id = await pool.fetchval(
        """
        SELECT s.id FROM label.statements s JOIN label.distributors d
        ON d.id = s.distributor_id WHERE d.dialect = 'kinetic_us' AND s.period = $1
        """,
        emitted,
    )

    def call(n: str, name: str, **arguments: object) -> MockTurn:
        return MockTurn(tool_calls=[ToolCall(id=n, name=name, arguments=dict(arguments))])

    script = [
        call("t1", "sql_query", query="SELECT count(*) AS n_artists FROM label.artists"),
        # The adversarial turn: reach for the answer key; the guardrail must kill it.
        call("t2", "sql_query", query="SELECT * FROM truth.expected_ledger"),
        call(
            "t3",
            "search_contracts",
            query="streaming royalty rate",
            artist=artist["stage_name"],
        ),
        call("t4", "read_clause", contract_id=contract_id, clause_no="§3"),
        call("t5", "calc_royalties", artist_id=artist["id"], period="2026-06"),
        call(
            "t6",
            "ingest_statement",
            path=f"data/inbox/kinetic_digital_{emitted}.csv",
        ),
        call("t7", "match_lines", statement_id=statement_id),
        call(
            "t8",
            "submit_batch",
            period=emitted,
            allocations=[{"artist_id": artist["id"], "net_payable": "101.01"}],
            flags=[{"kind": "unknown_isrc", "severity": "error", "payload": {}}],
        ),
        call("t9", "save_note", entity_ref="period:e2e", text="fresh drop reconciled"),
        call("t10", "recall_notes", entity_ref="period:e2e"),
        MockTurn(
            text="Reconciled the fresh drop end to end; batch proposed for review.",
            match="rejected by guardrails (sql_policy)",  # the model *saw* the denial
        ),
    ]

    settings = get_settings().model_copy(update={"data_dir": str(world_env.data_dir)})
    ctx = ToolContext(
        pool=pool,
        settings=settings,
        embedder=HashingEmbedder(),
        reranker=LexicalReranker(),
    )
    # PostgresSink is part of the production wiring — and app.notes.created_by
    # FK-references app.runs, so the stamped note needs the run row to exist.
    sink = InMemorySink()
    pg_sink = PostgresSink(world_env.database_url)
    runtime = AgentRuntime(
        providers={"mock": MockProvider(script)},
        registry=ModelRegistry.load(),
        tracer=Tracer([sink, pg_sink]),
    )
    agent = AgentSpec(
        name="e2e-all-tools",
        system_prompt="Exercise every tool once.",
        model="mock-sonnet",
        tools=build_all_tools(ctx),
        checks=[sql_policy_check],
        limits=RunLimits(max_iterations=12, run_budget_usd=Decimal("5")),
    )

    try:
        result = await runtime.run(agent, "Reconcile the new kinetic drop.")
    finally:
        await pg_sink.aclose()
    assert result.status == "completed"
    assert result.final is not None
    assert "batch proposed" in result.final.answer

    spans = sink.spans
    tool_spans = [s for s in spans if s.kind == "tool_call"]
    assert [s.attrs.get("tool") for s in tool_spans] == [
        "sql_query",
        "sql_query",
        "search_contracts",
        "read_clause",
        "calc_royalties",
        "ingest_statement",
        "match_lines",
        "submit_batch",
        "save_note",
        "recall_notes",
    ]
    guardrails = [s for s in spans if s.kind == "guardrail"]
    assert len(guardrails) == 1
    assert guardrails[0].attrs["kind"] == "sql_policy"
    assert "truth" in guardrails[0].attrs["detail"]
    denied = [s for s in tool_spans if s.attrs.get("status") == "denied"]
    assert len(denied) == 1  # only the truth query died
    errored = [s.name for s in tool_spans if s.attrs.get("status") == "error"]
    assert errored == []  # every other tool executed cleanly

    # The staging writes really happened, stamped with this run.
    batch = await pool.fetchrow(
        "SELECT id, submitted_by_run, status FROM staging.statement_batches "
        "ORDER BY id DESC LIMIT 1"
    )
    assert batch["submitted_by_run"] == result.run_id
    assert batch["status"] == "proposed"
    staged = await pool.fetchval(
        "SELECT count(*) FROM staging.ingested_lines WHERE statement_id = $1",
        statement_id,
    )
    assert isinstance(staged, int) and staged > 0
    ingest_stamp = await pool.fetchval(
        "SELECT DISTINCT ingested_by_run FROM staging.ingested_lines WHERE statement_id = $1",
        statement_id,
    )
    assert ingest_stamp == result.run_id
    note_stamp = await pool.fetchval(
        "SELECT created_by FROM app.notes WHERE entity_ref = 'period:e2e' ORDER BY id DESC LIMIT 1"
    )
    assert note_stamp == result.run_id
