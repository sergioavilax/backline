"""EvalRunner integration tests: real tools + seeded Postgres + scripted MockProvider.

Covers the §5 runner contract: eval_runs/eval_results rows per (question, tier), JSON
artifacts, budget refusal + hard-stop + resume, and the three tracks (platform / b0 /
b1) through one scoring path.
"""

import json
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any

import asyncpg
import pytest

from backline.providers.base import ToolCall
from backline.providers.mock import MockProvider, MockTurn
from backline.providers.registry import ModelRegistry
from backline.rag.embed import run_embed
from backline.rag.embedder import HashingEmbedder
from backline.rag.reranker import LexicalReranker
from evals.runner import BudgetRefused, EvalRunner, RunnerConfig, project_cost
from evals.types import Question, Suite, suite_hash
from tests.conftest import WorldEnv, requires_postgres

pytestmark = requires_postgres


@pytest.fixture(autouse=True)
async def chunks_ready(world_env: WorldEnv, pool: asyncpg.Pool) -> None:
    await run_embed(pool, data_dir=world_env.data_dir, embedder=HashingEmbedder())


async def _mini_suite(pool: asyncpg.Pool) -> tuple[Suite, dict[str, list[MockTurn]]]:
    """Three real questions against the seeded world + their perfect-agent scripts."""
    ledger = await pool.fetchrow(
        """
        SELECT el.artist_id, a.stage_name, el.period, el.net_payable
        FROM truth.expected_ledger el JOIN label.artists a ON a.id = el.artist_id
        WHERE el.net_payable > 100 ORDER BY el.artist_id, el.period LIMIT 1
        """
    )
    artist = await pool.fetchrow("SELECT id, stage_name FROM label.artists ORDER BY id LIMIT 1")
    n_tracks = await pool.fetchval(
        "SELECT count(*) FROM label.tracks WHERE primary_artist_id = $1", artist["id"]
    )
    sql = (
        "SELECT count(*) AS n FROM label.tracks t JOIN label.artists a "
        f"ON a.id = t.primary_artist_id WHERE a.id = {artist['id']}"
    )
    questions = [
        Question(
            id="mini-money-01",
            category="royalty_math",
            agent="counsel",
            tiers=["t1", "t2"],
            prompt=f"Net payable for {ledger['stage_name']} in {ledger['period']}?\n\n"
            "End your reply with a line exactly `ANSWER: $<amount>` (USD).",
            answer_kind="money",
            expected=str(ledger["net_payable"]),
            tolerance="0.01",
            t2_checks=["money_via_calculator"],
            meta={"artist_id": ledger["artist_id"], "period": ledger["period"]},
        ),
        Question(
            id="mini-abstain-01",
            category="abstention",
            agent="counsel",
            tiers=["t1"],
            prompt="What rate does Vera Nyx earn?\n\n"
            "End your reply with a line exactly `ANSWER: <rate>%`.",
            answer_kind="abstain",
            expected="ABSTAIN",
        ),
        Question(
            id="mini-sql-01",
            category="catalog_lookup",
            agent="analyst",
            tiers=["t1"],
            prompt=f"How many tracks does {artist['stage_name']} have?\n\n"
            "End your reply with a line exactly `ANSWER: <integer>`.",
            answer_kind="count",
            expected=int(n_tracks),
            meta={"reference_sql": sql},
        ),
    ]
    scripts = {
        "mini-money-01": [
            MockTurn(
                tool_calls=[
                    ToolCall(
                        id="c1",
                        name="calc_royalties",
                        arguments={
                            "artist_id": ledger["artist_id"],
                            "period": ledger["period"],
                        },
                    )
                ]
            ),
            MockTurn(
                text=f"Computed via the calculator.\nANSWER: ${ledger['net_payable']}",
                match="Royalty ledger",  # the real tool result reached the model
            ),
        ],
        "mini-abstain-01": [
            MockTurn(
                tool_calls=[
                    ToolCall(
                        id="s1",
                        name="search_contracts",
                        arguments={"query": "royalty rate", "artist": "Vera Nyx"},
                    )
                ]
            ),
            MockTurn(text="ABSTAIN: no artist named 'Vera Nyx' on the roster."),
        ],
        "mini-sql-01": [
            MockTurn(tool_calls=[ToolCall(id="q1", name="sql_query", arguments={"query": sql})]),
            MockTurn(text=f"Counted.\nANSWER: {n_tracks}", match=str(n_tracks)),
        ],
    }
    return (
        Suite(name="mini", world_seed=0, suite_hash=suite_hash(questions), questions=questions),
        scripts,
    )


def _runner(pool: asyncpg.Pool, scripts: dict[str, list[MockTurn]] | None = None) -> EvalRunner:
    def factory(question: Question) -> dict[str, Any]:
        assert scripts is not None and question.id in scripts, question.id
        return {"mock": MockProvider(scripts[question.id])}

    return EvalRunner(
        pool=pool,
        registry=ModelRegistry.load(),
        embedder=HashingEmbedder(),
        reranker=LexicalReranker(),
        provider_factory=factory if scripts is not None else None,
        providers=None if scripts is not None else {},
    )


def _config(suite: Suite, tmp_path: Path, **overrides: Any) -> RunnerConfig:
    defaults: dict[str, Any] = {
        "suite": suite,
        "model": "mock-sonnet",
        "track": "platform",
        "budget_usd": Decimal("5.00"),
        "assume_yes": True,
        "utility_model": "mock-haiku",
        "concurrency": 1,
        "out_dir": tmp_path / "evals-out",
        "data_dir": tmp_path,  # unused off-b0
    }
    defaults.update(overrides)
    return RunnerConfig(**defaults)


async def test_platform_run_scores_and_persists(pool: asyncpg.Pool, tmp_path: Path) -> None:
    suite, scripts = await _mini_suite(pool)
    runner = _runner(pool, scripts)
    summary = await runner.run(_config(suite, tmp_path))

    assert summary.n_scored == 3
    assert summary.t2_violations == 0
    assert summary.categories["royalty_math"]["score"] == 100.0
    assert summary.categories["abstention"]["score"] == 100.0
    assert summary.categories["catalog_lookup"]["score"] == 100.0
    assert summary.total_cost_usd > Decimal("0")

    run_row = await pool.fetchrow(
        "SELECT suite_hash, model, git_sha, finished_at, summary FROM app.eval_runs WHERE id = $1",
        summary.eval_run_id,
    )
    assert run_row is not None
    assert run_row["suite_hash"] == suite.suite_hash
    assert run_row["model"] == "mock-sonnet"
    assert run_row["finished_at"] is not None
    persisted = json.loads(run_row["summary"])
    assert persisted["categories"]["royalty_math"]["score"] == 100.0

    rows = await pool.fetch(
        "SELECT question_id, tier, score, passed FROM app.eval_results "
        "WHERE eval_run_id = $1 ORDER BY question_id, tier",
        summary.eval_run_id,
    )
    assert [(r["question_id"], r["tier"]) for r in rows] == [
        ("mini-abstain-01", "t1"),
        ("mini-money-01", "t1"),
        ("mini-money-01", "t2"),
        ("mini-sql-01", "t1"),
    ]
    assert all(r["passed"] for r in rows)

    artifact = summary.out_dir / "results.jsonl"
    lines = [json.loads(line) for line in artifact.read_text().splitlines()]
    assert {line["question_id"] for line in lines} == {
        "mini-money-01",
        "mini-abstain-01",
        "mini-sql-01",
    }
    assert (summary.out_dir / "summary.json").exists()
    # The answer key rode along into the truth schema.
    n_key = await pool.fetchval(
        "SELECT count(*) FROM truth.qa_answer_key WHERE question_id LIKE 'mini-%'"
    )
    assert n_key == 3


async def test_wrong_answer_scores_zero_not_error(pool: asyncpg.Pool, tmp_path: Path) -> None:
    suite, scripts = await _mini_suite(pool)
    scripts["mini-sql-01"] = [MockTurn(text="From memory, I believe it's 999.\nANSWER: 999")]
    runner = _runner(pool, scripts)
    summary = await runner.run(_config(suite, tmp_path))
    assert summary.categories["catalog_lookup"]["score"] == 0.0
    row = await pool.fetchrow(
        "SELECT score, passed, detail FROM app.eval_results "
        "WHERE eval_run_id = $1 AND question_id = 'mini-sql-01'",
        summary.eval_run_id,
    )
    assert row is not None
    assert not row["passed"] and Decimal(row["score"]) == 0
    assert json.loads(row["detail"])["extracted"] == "999"


async def test_budget_refusal_without_yes(pool: asyncpg.Pool, tmp_path: Path) -> None:
    suite, scripts = await _mini_suite(pool)
    runner = _runner(pool, scripts)
    config = _config(suite, tmp_path, budget_usd=Decimal("0.001"), assume_yes=False)
    projection = project_cost(suite.questions, config, ModelRegistry.load())
    assert projection > config.budget_usd
    before = await pool.fetchval(
        "SELECT count(*) FROM app.eval_runs WHERE suite_hash = $1", suite.suite_hash
    )
    with pytest.raises(BudgetRefused, match="exceeds budget"):
        await runner.run(config)
    # Nothing ran, nothing persisted by the refused call.
    after = await pool.fetchval(
        "SELECT count(*) FROM app.eval_runs WHERE suite_hash = $1", suite.suite_hash
    )
    assert after == before


async def test_budget_hard_stop_and_resume(pool: asyncpg.Pool, tmp_path: Path) -> None:
    suite, scripts = await _mini_suite(pool)
    runner = _runner(pool, scripts)
    # Mock usage is 120 in / 30 out on mock-sonnet pricing → ~$0.00081 per call, two
    # calls per question; a $0.001 budget with --yes exhausts after question one.
    first = await runner.run(_config(suite, tmp_path, budget_usd=Decimal("0.001"), assume_yes=True))
    assert first.budget_exhausted
    assert first.n_scored >= 1
    assert first.n_skipped_budget >= 1

    # Fresh scripts (the first runner consumed some) — resume completes the rest.
    suite2, scripts2 = await _mini_suite(pool)
    assert suite2.suite_hash == suite.suite_hash
    resumed_runner = _runner(pool, scripts2)
    resumed = await resumed_runner.run(
        _config(
            suite2,
            tmp_path,
            budget_usd=Decimal("5.00"),
            assume_yes=True,
            resume_run_id=first.eval_run_id,
        )
    )
    assert resumed.eval_run_id == first.eval_run_id
    assert resumed.n_scored == 3
    assert not resumed.budget_exhausted
    # No duplicate scoring: one row per (question, tier).
    rows = await pool.fetch(
        "SELECT question_id, tier, count(*) AS n FROM app.eval_results "
        "WHERE eval_run_id = $1 GROUP BY question_id, tier",
        first.eval_run_id,
    )
    assert all(r["n"] == 1 for r in rows)


async def test_resume_mismatch_rejected(pool: asyncpg.Pool, tmp_path: Path) -> None:
    suite, scripts = await _mini_suite(pool)
    runner = _runner(pool, scripts)
    with pytest.raises(ValueError, match="no eval run"):
        await runner.run(_config(suite, tmp_path, resume_run_id=uuid.uuid4()))


async def test_harness_error_recorded_not_fatal(pool: asyncpg.Pool, tmp_path: Path) -> None:
    suite, scripts = await _mini_suite(pool)
    scripts["mini-money-01"] = []  # empty script → ProviderError mid-run
    runner = _runner(pool, scripts)
    summary = await runner.run(_config(suite, tmp_path))
    assert summary.n_scored == 3  # the broken question still produced scored rows
    row = await pool.fetchrow(
        "SELECT passed, detail FROM app.eval_results "
        "WHERE eval_run_id = $1 AND question_id = 'mini-money-01' AND tier = 't1'",
        summary.eval_run_id,
    )
    assert row is not None and not row["passed"]
    # Runtime turned the exhausted mock into an errored run; the scorer recorded it.
    detail = json.loads(row["detail"])
    assert detail.get("failure") in {"run_error", "harness_error"}


# ── baseline tracks through the same runner ──────────────────────────────────


async def test_b0_track_packs_and_scores(
    pool: asyncpg.Pool, world_env: WorldEnv, tmp_path: Path
) -> None:
    suite, _ = await _mini_suite(pool)
    money_q = suite.questions[0]
    scripts = {
        money_q.id: [
            MockTurn(
                text=f"Reading statements... ANSWER: ${money_q.expected}\n"
                f"ANSWER: ${money_q.expected}"
            )
        ],
        "mini-abstain-01": [MockTurn(text="ABSTAIN: not in the materials.")],
        "mini-sql-01": [MockTurn(text="I count 4.\nANSWER: 4")],
    }
    runner = _runner(pool, scripts)
    summary = await runner.run(
        _config(
            suite,
            tmp_path,
            track="b0",
            data_dir=world_env.data_dir,
            pack_tokens=8000,
        )
    )
    assert summary.track == "b0"
    assert summary.n_scored == 3
    # The abstention question passes (abstaining is right for b0 too); packing meta
    # recorded per question; t2 recorded as not-applicable, never violated.
    assert summary.categories["abstention"]["score"] == 100.0
    assert summary.t2_violations == 0
    lines = [
        json.loads(line) for line in (summary.out_dir / "results.jsonl").read_text().splitlines()
    ]
    money_line = next(line for line in lines if line["question_id"] == money_q.id)
    assert money_line["tiers"]["t2"]["not_applicable"] == "b0"


async def test_b1_track_retrieves_and_scores(pool: asyncpg.Pool, tmp_path: Path) -> None:
    suite, _ = await _mini_suite(pool)
    scripts = {
        "mini-money-01": [MockTurn(text="ABSTAIN: the clauses carry no statement data.")],
        "mini-abstain-01": [MockTurn(text="ABSTAIN: no such artist in the clauses.")],
        "mini-sql-01": [MockTurn(text="ABSTAIN: cannot count a catalog from clauses.")],
    }
    runner = _runner(pool, scripts)
    summary = await runner.run(_config(suite, tmp_path, track="b1"))
    assert summary.track == "b1"
    assert summary.n_scored == 3
    # Honest baseline behavior: right on abstention, zero on the data questions.
    assert summary.categories["abstention"]["score"] == 100.0
    assert summary.categories["royalty_math"]["score"] == 0.0
    lines = [
        json.loads(line) for line in (summary.out_dir / "results.jsonl").read_text().splitlines()
    ]
    money_line = next(line for line in lines if line["question_id"] == "mini-money-01")
    assert money_line["tiers"]["t1"]["failure"] == "abstained_unexpectedly"
