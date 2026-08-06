"""Canonical agent flows on MockProvider against the seeded world (Phase 4 DoD).

Counsel cites clauses (and abstains honestly, and survives the injection canary);
Analyst answers a simple ask in one SQL round trip; the Reconciler drives
ingest → match → scan → allocations → submit_batch on a fresh month and *stops* —
no approval path exists. Real tools, real Postgres, scripted model turns; zero
network. Skips without DATABASE_URL.
"""

import os
import subprocess
import sys
from typing import Any

import asyncpg
import pytest

from backline.agents.configs import ReconcilerAnswer, build_agent
from backline.agents.promptfiles import load_prompt
from backline.config import get_settings
from backline.core.runtime import AgentRuntime, RunResult
from backline.core.trace import InMemorySink, Tracer
from backline.providers.base import ToolCall
from backline.providers.mock import MockProvider, MockTurn
from backline.providers.registry import ModelRegistry
from backline.rag.embed import run_embed
from backline.rag.embedder import HashingEmbedder
from backline.rag.reranker import LexicalReranker
from backline.tools.context import ToolContext
from tests.conftest import REPO_ROOT, WorldEnv, requires_postgres

pytestmark = requires_postgres

EMIT_PERIOD = "2026-07"
CANARY_CONTRACT = 670  # FBR-C-00670 §7 carries the seeded injection canary


@pytest.fixture
def ctx(pool: asyncpg.Pool, world_env: WorldEnv) -> ToolContext:
    settings = get_settings().model_copy(update={"data_dir": str(world_env.data_dir)})
    # Deterministic offline retrieval stack (D-011) — no model downloads in tests.
    return ToolContext(
        pool=pool,
        settings=settings,
        embedder=HashingEmbedder(),
        reranker=LexicalReranker(),
    )


@pytest.fixture(autouse=True)
async def chunks_ready(world_env: WorldEnv, pool: asyncpg.Pool) -> None:
    """Retrieval tools need the clause store; reconcile is idempotent and cheap."""
    await run_embed(pool, data_dir=world_env.data_dir, embedder=HashingEmbedder())


def _call(name: str, id_: str, **arguments: Any) -> ToolCall:
    return ToolCall(id=id_, name=name, arguments=dict(arguments))


def _run(
    provider: MockProvider, agent_name: str, ctx: ToolContext
) -> tuple[AgentRuntime, InMemorySink, Any]:
    sink = InMemorySink()
    runtime = AgentRuntime(
        providers={"mock": provider},
        registry=ModelRegistry.load(),
        tracer=Tracer([sink]),
    )
    agent = build_agent(agent_name, ctx, model="mock-sonnet", utility_model="mock-haiku")
    return runtime, sink, agent


def _tool_span_names(sink: InMemorySink) -> list[str]:
    return [s.attrs["tool"] for s in sink.spans if s.kind == "tool_call"]


# ── Counsel ──────────────────────────────────────────────────────────────────


async def test_counsel_canonical_flow_cites_clauses(ctx: ToolContext, pool: asyncpg.Pool) -> None:
    artist = await pool.fetchrow(
        """
        SELECT a.id, a.stage_name, c.id AS base_id
        FROM label.artists a
        JOIN label.contracts c ON c.artist_id = a.id AND c.kind = 'base'
        WHERE NOT EXISTS (SELECT 1 FROM label.contracts x
                          WHERE x.artist_id = a.id AND x.id <> c.id)
        ORDER BY a.id LIMIT 1
        """
    )
    citation = f"FBR-C-{artist['base_id']:05d} §3"
    provider = MockProvider(
        [
            MockTurn(
                text="Searching the governing documents.",
                tool_calls=[
                    _call(
                        "search_contracts",
                        "c1",
                        query="royalty rate streaming",
                        artist=artist["stage_name"],
                    )
                ],
                match=f"What royalty rate does {artist['stage_name']} earn on streaming?",
            ),
            MockTurn(
                text="Verifying the exact wording.",
                tool_calls=[
                    _call("read_clause", "c2", contract_id=artist["base_id"], clause_no="§3")
                ],
                match="Cite as `CODE §N`",  # the search result reached the model
            ),
            MockTurn(
                text=(
                    f"{artist['stage_name']} earns the digital streaming rate in "
                    f"{citation} (verified verbatim)."
                ),
                match="ROYALTIES",  # the clause body reached the model
            ),
        ]
    )
    runtime, sink, agent = _run(provider, "counsel", ctx)

    result = await runtime.run(
        agent, f"What royalty rate does {artist['stage_name']} earn on streaming?"
    )

    assert result.status == "completed"
    assert result.final is not None
    assert [c.ref for c in result.final.citations] == [citation]
    assert result.final.abstained is False
    assert _tool_span_names(sink) == ["search_contracts", "read_clause"]
    run = sink.runs[0]
    assert run.agent == "counsel"
    assert run.meta["prompt_sha256"] == load_prompt("counsel").short_hash
    # The system prompt the model saw is the versioned file, verbatim.
    assert provider.calls[0].system == load_prompt("counsel").text


async def test_counsel_abstains_on_unknown_artist(ctx: ToolContext) -> None:
    provider = MockProvider(
        [
            MockTurn(
                tool_calls=[
                    _call(
                        "search_contracts",
                        "c1",
                        query="royalty rate",
                        artist="Vera Nyx",
                    )
                ]
            ),
            MockTurn(
                text="ABSTAIN: no artist named 'Vera Nyx' on the roster.",
                match="no artist",  # the miss (with candidates) reached the model
            ),
        ]
    )
    runtime, _sink, agent = _run(provider, "counsel", ctx)

    result = await runtime.run(agent, "What is Vera Nyx's royalty rate?")

    assert result.status == "completed"
    assert result.final is not None
    assert result.final.abstained is True
    assert result.final.citations == []


async def test_counsel_injection_canary_is_flagged_not_obeyed(ctx: ToolContext) -> None:
    provider = MockProvider(
        [
            MockTurn(
                tool_calls=[
                    _call(
                        "read_clause",
                        "c1",
                        contract_id=CANARY_CONTRACT,
                        clause_no="§7",
                    )
                ]
            ),
            MockTurn(
                text=(
                    "FBR-C-00670 §7 contains instruction-shaped text that is not a "
                    "contractual term; I ignored it. The clause carries no rate "
                    "or obligation to report."
                ),
                match="guardrail notice — injection_suspected",
            ),
        ]
    )
    runtime, sink, agent = _run(provider, "counsel", ctx)

    result = await runtime.run(agent, "What does contract 670 clause 7 say?")

    assert result.status == "completed"
    guardrail = [s for s in sink.spans if s.kind == "guardrail"]
    assert [s.attrs["kind"] for s in guardrail] == ["injection_suspected"]
    assert guardrail[0].attrs["tool"] == "read_clause"
    # The canary text still reached the model (flag, don't block), annotated.
    tool_msg = provider.calls[1].messages[-1]
    assert tool_msg.content.startswith("[guardrail notice — injection_suspected")
    assert "SYSTEM OVERRIDE" in tool_msg.content
    assert tool_msg.is_error is False


# ── Analyst ──────────────────────────────────────────────────────────────────


async def test_analyst_simple_ask_is_one_sql_round_trip(
    ctx: ToolContext, pool: asyncpg.Pool
) -> None:
    artist = await pool.fetchrow("SELECT id, stage_name FROM label.artists ORDER BY id LIMIT 1")
    n_tracks = await pool.fetchval(
        "SELECT count(*) FROM label.tracks WHERE primary_artist_id = $1", artist["id"]
    )
    query = (
        "SELECT count(*) AS n_tracks FROM label.tracks t "
        "JOIN label.artists a ON a.id = t.primary_artist_id "
        f"WHERE a.stage_name = '{artist['stage_name']}'"
    )
    provider = MockProvider(
        [
            MockTurn(tool_calls=[_call("sql_query", "q1", query=query)]),
            MockTurn(
                text=(f"{artist['stage_name']} has {n_tracks} tracks.\n\n```sql\n{query}\n```"),
                match=str(n_tracks),  # the real result reached the model
            ),
        ]
    )
    runtime, sink, agent = _run(provider, "analyst", ctx)

    result = await runtime.run(agent, f"How many tracks does {artist['stage_name']} have?")

    assert result.status == "completed"
    assert _tool_span_names(sink) == ["sql_query"]  # exactly one round trip
    assert result.final is not None
    assert str(n_tracks) in result.final.answer


async def test_analyst_truth_query_dies_as_guardrail_and_recovers(ctx: ToolContext) -> None:
    provider = MockProvider(
        [
            MockTurn(
                tool_calls=[_call("sql_query", "q1", query="SELECT * FROM truth.expected_ledger")]
            ),
            MockTurn(
                text="ABSTAIN: that table is not part of the label's business schemas.",
                match="rejected by guardrails",
            ),
        ]
    )
    runtime, sink, agent = _run(provider, "analyst", ctx)

    result = await runtime.run(agent, "Dump truth.expected_ledger for me")

    assert result.status == "completed"
    assert result.final is not None and result.final.abstained is True
    guardrail = [s for s in sink.spans if s.kind == "guardrail"]
    assert [s.attrs["kind"] for s in guardrail] == ["sql_policy"]


# ── Reconciler ───────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def emitted(world_env: WorldEnv) -> str:
    result = subprocess.run(
        [sys.executable, "-m", "datagen", "emit-period", EMIT_PERIOD],
        cwd=REPO_ROOT,
        env={
            **os.environ,
            "DATABASE_URL": world_env.database_url,
            "DATA_DIR": str(world_env.data_dir),
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=600,
    )
    assert result.returncode == 0, f"emit-period failed:\n{result.stdout}\n{result.stderr}"
    return EMIT_PERIOD


async def test_reconciler_workflow_produces_batch_and_stops(
    emitted: str, ctx: ToolContext, pool: asyncpg.Pool
) -> None:
    statement = await pool.fetchrow(
        """
        SELECT s.id FROM label.statements s
        JOIN label.distributors d ON d.id = s.distributor_id
        WHERE s.period = $1 AND d.dialect = 'kinetic_us'
        """,
        emitted,
    )
    drop_path = f"data/inbox/kinetic_digital_{emitted}.csv"
    provider = MockProvider(
        [
            MockTurn(
                text="Ingesting the drop.",
                tool_calls=[_call("ingest_statement", "t1", path=drop_path)],
                match=f"Reconcile the kinetic drop for {emitted}",
            ),
            MockTurn(
                text="Matching to catalog.",
                tool_calls=[_call("match_lines", "t2", statement_id=statement["id"])],
                match="staged into staging.ingested_lines",
            ),
            MockTurn(
                text="Scanning for anomalies.",
                tool_calls=[_call("scan_anomalies", "t3", period=emitted)],
                match="matched to catalog",
            ),
            MockTurn(
                text="Computing allocations.",
                tool_calls=[
                    _call(
                        "compute_allocations",
                        "t4",
                        period=emitted,
                        include_staged=True,
                        min_net_payable="100",
                    )
                ],
                match="Anomaly scan",
            ),
            MockTurn(
                text="Submitting for review.",
                tool_calls=[
                    _call(
                        "submit_batch",
                        "t5",
                        period=emitted,
                        allocations=[
                            {
                                "artist_id": 49,
                                "net_payable": "1234.56",
                                "line_detail": {"source": "compute_allocations"},
                            },
                            {"artist_id": 28, "net_payable": "987.65"},
                        ],
                        flags=[
                            {
                                "kind": "unknown_isrc",
                                "severity": "error",
                                "payload": {"note": "unmatched lines in the drop"},
                            },
                            {
                                "kind": "period_bleed",
                                "severity": "warning",
                                "payload": {"note": "late-reported prior-month line"},
                            },
                        ],
                        note="kinetic drop only; staged lines pending human approval",
                    )
                ],
                match="Proposed allocations",
            ),
            MockTurn(
                text=(
                    "BATCH: 12345\nFLAGS: 2 (error: 1, warning: 1)\n"
                    "Reconciled the kinetic drop; batch awaits human review."
                ),
                match="Submitted batch",
            ),
        ]
    )
    runtime, sink, agent = _run(provider, "reconciler", ctx)

    result: RunResult = await runtime.run(
        agent, f"Reconcile the kinetic drop for {emitted} and submit it for review."
    )

    assert result.status == "completed"
    # The canonical workflow ran in order, then the run STOPPED at the final text.
    assert _tool_span_names(sink) == [
        "ingest_statement",
        "match_lines",
        "scan_anomalies",
        "compute_allocations",
        "submit_batch",
    ]

    final = result.final
    assert isinstance(final, ReconcilerAnswer)
    assert final.batch_id == 12345  # parsed from the wrap-up protocol
    assert final.flags_summary == "2 (error: 1, warning: 1)"

    # The real batch: proposed, stamped with this run, flags attached — and nothing
    # in the platform promoted it.
    batch = await pool.fetchrow(
        "SELECT id, status, summary FROM staging.statement_batches WHERE submitted_by_run = $1",
        result.run_id,
    )
    assert batch is not None
    assert batch["status"] == "proposed"
    allocations = await pool.fetch(
        "SELECT artist_id, net_payable FROM staging.proposed_allocations WHERE batch_id = $1",
        batch["id"],
    )
    assert {(r["artist_id"], str(r["net_payable"])) for r in allocations} == {
        (49, "1234.560000"),
        (28, "987.650000"),
    }
    flags = await pool.fetch(
        "SELECT kind, severity FROM staging.flags WHERE batch_id = $1 ORDER BY kind",
        batch["id"],
    )
    assert [(r["kind"], r["severity"]) for r in flags] == [
        ("period_bleed", "warning"),
        ("unknown_isrc", "error"),
    ]
    # Invariant 5: the statement is still 'received'; label saw nothing.
    status = await pool.fetchval(
        "SELECT status FROM label.statements WHERE id = $1", statement["id"]
    )
    assert status == "received"
