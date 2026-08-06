"""End-to-end sweep tests against the seeded world (Phase 7 DoD): the row runner
drives the real eval harness, distills trace-derived metrics into the results
document, survives a budget stop with a working resume, and self-heals stale
sweep state — all on scripted MockProviders, keylessly.

The suite under test is the core suite's smoke slice re-wrapped as a *full* suite
(10 questions, own content hash): the sweep's full-run semantics — results
written, state cleared, report generated — are exercised without test-only flags
in production code, and without a 133-question mock scripting burden."""

import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any

import asyncpg
import pytest

from backline.config import Settings, get_settings
from backline.core.trace import PostgresSink
from backline.providers.base import Provider
from backline.providers.mock import MockProvider
from backline.providers.registry import ModelRegistry
from backline.rag.embed import run_embed
from backline.rag.embedder import HashingEmbedder
from backline.rag.reranker import LexicalReranker
from benchmarks.report import write_report
from benchmarks.sweep import (
    SweepContext,
    SweepMatrix,
    SweepRow,
    load_results_doc,
    load_state,
    preflight_world,
    run_row,
    save_state,
)
from evals.runner import EvalRunner
from evals.smoke import build_judge_script, build_platform_script
from evals.types import Question, Suite, load_suite, suite_hash
from tests.conftest import WorldEnv, requires_postgres

pytestmark = requires_postgres

JUDGE_MODEL = "mock-sonnet"


@pytest.fixture(autouse=True)
async def chunks_ready(world_env: WorldEnv, pool: asyncpg.Pool) -> None:
    await run_embed(pool, data_dir=world_env.data_dir, embedder=HashingEmbedder())


@pytest.fixture
def smoke_slice() -> Suite:
    core = load_suite("core")
    questions = core.subset("smoke")
    return Suite(
        name="core-smoke-slice",
        world_seed=core.world_seed,
        suite_hash=suite_hash(questions),
        questions=questions,
    )


@pytest.fixture
def settings(world_env: WorldEnv) -> Settings:
    return get_settings().model_copy(update={"data_dir": str(world_env.data_dir)})


@pytest.fixture
async def pg_sink(world_env: WorldEnv) -> Any:
    sink = PostgresSink(world_env.database_url)
    yield sink
    await sink.aclose()


def _matrix(*rows: SweepRow) -> SweepMatrix:
    return SweepMatrix(suite="core", track="platform", judge_model=JUDGE_MODEL, rows=list(rows))


def _state_file(tmp_path: Path) -> Path:
    return tmp_path / "state.json"


def _context(
    pool: asyncpg.Pool,
    settings: Settings,
    smoke_slice: Suite,
    matrix: SweepMatrix,
    pg_sink: PostgresSink,
    tmp_path: Path,
) -> SweepContext:
    def platform_factory(question: Question) -> dict[str, Provider]:
        return {"mock": MockProvider(build_platform_script(question))}

    def judge_factory(question: Question) -> dict[str, Provider]:
        return {"mock": MockProvider(build_judge_script(question))}

    runner = EvalRunner(
        pool=pool,
        registry=ModelRegistry.load(),
        settings=settings,
        embedder=None,  # resolve from the chunk store's recorded model
        reranker=LexicalReranker(),
        provider_factory=platform_factory,
        judge_provider_factory=judge_factory,
        extra_sinks=[pg_sink],  # metrics come from app.runs/app.spans
    )
    return SweepContext(
        pool=pool,
        registry=ModelRegistry.load(),
        settings=settings,
        suite=smoke_slice,
        matrix=matrix,
        runner=runner,
        results_dir=tmp_path / "results",
        state_file=_state_file(tmp_path),
        out_dir=tmp_path / "evals",
        concurrency=1,  # scripted spends accrue deterministically
    )


async def test_world_preflight_passes_on_the_seeded_world(pool: asyncpg.Pool) -> None:
    assert await preflight_world(pool) == []


async def test_full_row_produces_the_results_document_and_report(
    pool: asyncpg.Pool,
    settings: Settings,
    smoke_slice: Suite,
    pg_sink: PostgresSink,
    tmp_path: Path,
) -> None:
    row = SweepRow(model="mock-sonnet", budget_usd=Decimal("1.00"))
    matrix = _matrix(row)
    ctx = _context(pool, settings, smoke_slice, matrix, pg_sink, tmp_path)

    outcome = await run_row(ctx, row, assume_yes=True)
    assert outcome.complete
    assert outcome.path == ctx.results_dir / "mock-sonnet.json"
    doc = load_results_doc(outcome.path)

    # Accuracy: perfect scripts score 100 in every category (mirrors eval-smoke).
    assert doc["n_scored"] == doc["n_questions"] == 10
    assert doc["complete"] is True and doc["overall_score"] == 100.0
    assert len(doc["categories"]) == 10
    assert doc["t2_violations"] == 0

    # Trace-derived metrics: every scripted run completed, called tools, and
    # burned iterations/tokens — and none of it errored.
    assert doc["runs"] == {"n": 10, "completed": 10, "exhausted": 0, "error": 0}
    assert doc["iterations_mean"] > 1.0
    assert doc["tool_calls"]["n"] >= 10
    assert doc["tool_calls"]["error_rate"] == 0.0
    assert doc["tokens"]["input"] > 0 and doc["tokens"]["output"] > 0

    # Money: judge spend (T3 questions exist in the slice) is split out of
    # $/query, and the split reconciles exactly with the metered total.
    agent, judge, total = (
        Decimal(doc["agent_cost_usd"]),
        Decimal(doc["judge_cost_usd"]),
        Decimal(doc["total_cost_usd"]),
    )
    assert agent > 0 and judge > 0 and agent + judge == total
    assert doc["usd_per_query"] == str((agent / 10).quantize(Decimal("0.0001")))

    # Sweep bookkeeping: state cleared on completion, report renders the row.
    assert load_state(_state_file(tmp_path)) == {}
    report_path = write_report(matrix, ctx.results_dir)
    text = report_path.read_text(encoding="utf-8")
    assert "| mock-sonnet |" in text and "100.0" in text
    assert (ctx.results_dir / "comparison.svg").exists()


async def test_budget_stop_writes_a_partial_row_and_resume_completes_it(
    pool: asyncpg.Pool,
    settings: Settings,
    smoke_slice: Suite,
    pg_sink: PostgresSink,
    tmp_path: Path,
) -> None:
    row = SweepRow(model="mock-haiku", budget_usd=Decimal("1.00"))
    matrix = _matrix(row)
    ctx = _context(pool, settings, smoke_slice, matrix, pg_sink, tmp_path)

    # Mock turns bill real (tiny) usage, so a micro-budget trips the runner's
    # hard stop mid-suite — the unattended-sweep failure mode under test.
    first = await run_row(ctx, row, budget_override=Decimal("0.003"), assume_yes=True)
    assert not first.complete
    assert 0 < first.summary.n_scored < 10
    assert first.summary.n_skipped_budget > 0
    partial_doc = load_results_doc(ctx.results_dir / "mock-haiku.json")
    assert partial_doc["complete"] is False
    assert partial_doc["budget_exhausted"] is True

    # The interrupted row stays resumable: state still points at its eval run…
    state = load_state(_state_file(tmp_path))
    assert state["mock-haiku"]["eval_run_id"] == str(first.summary.eval_run_id)

    # …and re-running the row finishes the skipped questions under a real budget,
    # inside the SAME eval run, leaving a complete document and no state.
    second = await run_row(ctx, row, assume_yes=True)
    assert second.complete
    assert second.summary.eval_run_id == first.summary.eval_run_id
    final_doc = load_results_doc(ctx.results_dir / "mock-haiku.json")
    assert final_doc["complete"] is True
    assert final_doc["n_scored"] == 10
    assert final_doc["runs"]["n"] == 10  # aggregates span both sessions
    assert load_state(_state_file(tmp_path)) == {}


async def test_stale_sweep_state_self_heals(
    pool: asyncpg.Pool,
    settings: Settings,
    smoke_slice: Suite,
    pg_sink: PostgresSink,
    tmp_path: Path,
) -> None:
    row = SweepRow(model="mock-sonnet", budget_usd=Decimal("1.00"))
    matrix = _matrix(row)
    ctx = _context(pool, settings, smoke_slice, matrix, pg_sink, tmp_path)

    # A state entry whose eval run the database has never heard of (reset DB,
    # copied-over data dir): the sweep must start fresh, not crash the night run.
    planted = str(uuid.uuid4())
    save_state(
        _state_file(tmp_path),
        {
            "mock-sonnet": {
                "eval_run_id": planted,
                "suite_hash": smoke_slice.suite_hash,
                "track": "platform",
                "started_at": "2026-08-06T00:00:00+00:00",
            }
        },
    )
    outcome = await run_row(ctx, row, assume_yes=True)
    assert outcome.complete
    assert str(outcome.summary.eval_run_id) != planted
    assert load_state(_state_file(tmp_path)) == {}
