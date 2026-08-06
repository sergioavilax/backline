"""EvalRunner integration tests: real tools + seeded Postgres + scripted MockProvider.

Covers the §5 runner contract: eval_runs/eval_results rows per (question, tier), JSON
artifacts, budget refusal + hard-stop + resume, and the three tracks (platform / b0 /
b1) through one scoring path.
"""

import asyncio
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


class _SlowProvider:
    """Wraps a MockProvider, holding every completion open — the question's cost
    stays in flight, invisible to any gate that reads only landed spend."""

    name = "mock"

    def __init__(self, inner: MockProvider, delay_s: float) -> None:
        self._inner = inner
        self._delay_s = delay_s

    async def complete(self, req: Any) -> Any:
        await asyncio.sleep(self._delay_s)
        return await self._inner.complete(req)


async def test_budget_hard_stop_trips_while_costs_are_in_flight(
    pool: asyncpg.Pool, tmp_path: Path
) -> None:
    """Regression for run 2b9f39fb: with concurrency > 1 the gate must count
    in-flight reservations. Landed cost alone let all 133 questions start — slow
    expensive questions (p95 115s) held their cost invisibly while cheap ones
    (p50 15s) sailed through the landed-only check, so the hard stop never fired."""
    suite, scripts = await _mini_suite(pool)
    slow_id = suite.questions[0].id

    def factory(question: Question) -> dict[str, Any]:
        provider = MockProvider(scripts[question.id])
        if question.id == slow_id:
            return {"mock": _SlowProvider(provider, delay_s=0.25)}
        return {"mock": provider}

    runner = EvalRunner(
        pool=pool,
        registry=ModelRegistry.load(),
        embedder=HashingEmbedder(),
        reranker=LexicalReranker(),
        provider_factory=factory,
    )
    # The budget is below a single counsel-question reservation: the first question
    # may start (some budget remains — the pre-existing semantics), and everything
    # else must skip while its cost is still in flight, at any concurrency.
    summary = await runner.run(
        _config(suite, tmp_path, budget_usd=Decimal("0.02"), assume_yes=True, concurrency=4)
    )
    assert summary.budget_exhausted
    assert summary.n_scored == 1
    assert summary.n_skipped_budget == len(suite.questions) - 1


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


async def test_categories_filter_runs_targeted_subset(pool: asyncpg.Pool, tmp_path: Path) -> None:
    """`--categories` scopes a run to named categories — targeted re-runs after a
    harness fix are priced per question, not per suite."""
    suite, scripts = await _mini_suite(pool)
    runner = _runner(pool, scripts)
    summary = await runner.run(_config(suite, tmp_path, categories=("abstention",)))
    assert summary.n_questions == 1
    assert summary.n_scored == 1
    assert set(summary.categories) == {"abstention"}
    assert summary.categories["abstention"]["score"] == 100.0

    with pytest.raises(ValueError, match="unknown categories"):
        await runner.run(_config(suite, tmp_path, categories=("nope",)))


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


# ── infra-error quarantine + --retry-errors (D-032) ──────────────────────────


async def test_retry_errors_heals_infra_errored_rows_and_recomputes_summary(
    pool: asyncpg.Pool, tmp_path: Path
) -> None:
    """The Opus mid-run outage in miniature: a provider dies on one question, the
    question is quarantined — visible in the errors bucket, excluded from category
    accuracy — and a --retry-errors resume supersedes exactly those rows and
    re-executes them inside the same eval run."""
    suite, scripts = await _mini_suite(pool)
    scripts["mini-money-01"] = []  # provider outage: ProviderError → run status "error"
    runner = _runner(pool, scripts)
    first = await runner.run(_config(suite, tmp_path))

    assert first.n_scored == 3  # the errored question has rows…
    assert first.errors == {
        "n": 1,
        "question_ids": ["mini-money-01"],
        "by_category": {"royalty_math": 1},
    }
    assert "royalty_math" not in first.categories  # …but is no measurement
    assert first.categories["abstention"]["score"] == 100.0
    assert first.categories["catalog_lookup"]["score"] == 100.0
    # The dead run's failed trace assertions must not read as process violations.
    assert first.t2_violations == 0
    row = await pool.fetchrow(
        "SELECT detail FROM app.eval_results "
        "WHERE eval_run_id = $1 AND question_id = 'mini-money-01' AND tier = 't1'",
        first.eval_run_id,
    )
    assert row is not None
    assert json.loads(row["detail"])["failure"] in {"run_error", "harness_error"}

    clean_row_ids = {
        r["id"]
        for r in await pool.fetch(
            "SELECT id FROM app.eval_results "
            "WHERE eval_run_id = $1 AND question_id != 'mini-money-01'",
            first.eval_run_id,
        )
    }

    # The heal pass: provider recovered (fresh perfect scripts), same eval run.
    suite2, scripts2 = await _mini_suite(pool)
    healed = await _runner(pool, scripts2).run(
        _config(suite2, tmp_path, resume_run_id=first.eval_run_id, retry_errors=True)
    )
    assert healed.eval_run_id == first.eval_run_id
    assert healed.errors == {"n": 0, "question_ids": [], "by_category": {}}
    assert healed.n_scored == 3
    assert healed.categories["royalty_math"]["score"] == 100.0
    assert healed.categories["abstention"]["score"] == 100.0

    # Superseded, not appended: one row per (question, tier), and the
    # legitimately-scored rows are the *same rows* (primary keys), not rewrites.
    rows = await pool.fetch(
        "SELECT id, question_id, tier FROM app.eval_results WHERE eval_run_id = $1",
        first.eval_run_id,
    )
    keys = [(r["question_id"], r["tier"]) for r in rows]
    assert len(keys) == len(set(keys))
    assert clean_row_ids <= {r["id"] for r in rows}

    # The artifact mirrors the healed state: one line per question, money healed.
    lines = [
        json.loads(line) for line in (healed.out_dir / "results.jsonl").read_text().splitlines()
    ]
    assert sorted(line["question_id"] for line in lines) == [
        "mini-abstain-01",
        "mini-money-01",
        "mini-sql-01",
    ]
    money_line = next(line for line in lines if line["question_id"] == "mini-money-01")
    assert money_line["score"] == 1.0
    summary_doc = json.loads((healed.out_dir / "summary.json").read_text())
    assert summary_doc["errors"]["n"] == 0


async def test_retry_errors_spares_legitimate_failures(pool: asyncpg.Pool, tmp_path: Path) -> None:
    """Only infra failures re-run. A wrong answer and an iteration-capped run are
    model behavior — the retry pass must leave their rows byte-untouched."""
    suite, scripts = await _mini_suite(pool)
    scripts["mini-money-01"] = []  # infra: run_error
    scripts["mini-sql-01"] = [MockTurn(text="From memory, 999.\nANSWER: 999")]  # legit wrong
    scripts["mini-abstain-01"] = [  # legit run_exhausted: tool-calls past the cap
        MockTurn(
            tool_calls=[
                ToolCall(
                    id=f"s{i}",
                    name="search_contracts",
                    arguments={"query": "royalty rate", "artist": "Vera Nyx"},
                )
            ]
        )
        for i in range(14)
    ]
    first = await _runner(pool, scripts).run(_config(suite, tmp_path))
    assert first.errors["question_ids"] == ["mini-money-01"]  # exhausted ≠ infra
    assert first.categories["abstention"]["score"] == 0.0
    assert first.categories["catalog_lookup"]["score"] == 0.0
    exhausted_row = await pool.fetchrow(
        "SELECT id, detail FROM app.eval_results "
        "WHERE eval_run_id = $1 AND question_id = 'mini-abstain-01' AND tier = 't1'",
        first.eval_run_id,
    )
    assert exhausted_row is not None
    assert json.loads(exhausted_row["detail"])["failure"] == "run_exhausted"
    legit_ids = {
        r["id"]
        for r in await pool.fetch(
            "SELECT id FROM app.eval_results "
            "WHERE eval_run_id = $1 AND question_id != 'mini-money-01'",
            first.eval_run_id,
        )
    }

    suite2, scripts2 = await _mini_suite(pool)  # all-perfect scripts this time
    healed = await _runner(pool, scripts2).run(
        _config(suite2, tmp_path, resume_run_id=first.eval_run_id, retry_errors=True)
    )
    assert healed.errors["n"] == 0
    assert healed.categories["royalty_math"]["score"] == 100.0  # healed
    assert healed.categories["catalog_lookup"]["score"] == 0.0  # still the model's miss
    assert healed.categories["abstention"]["score"] == 0.0  # still the model's cap-out
    after_ids = {
        r["id"]
        for r in await pool.fetch(
            "SELECT id FROM app.eval_results "
            "WHERE eval_run_id = $1 AND question_id != 'mini-money-01'",
            first.eval_run_id,
        )
    }
    assert after_ids == legit_ids


async def test_retry_errors_requires_a_resume_run(pool: asyncpg.Pool, tmp_path: Path) -> None:
    suite, scripts = await _mini_suite(pool)
    with pytest.raises(ValueError, match="retry_errors needs a run to heal"):
        await _runner(pool, scripts).run(_config(suite, tmp_path, retry_errors=True))


async def test_retry_errors_scoped_by_the_categories_filter(
    pool: asyncpg.Pool, tmp_path: Path
) -> None:
    """A filtered heal supersedes only rows it will re-execute. Errored rows outside
    the filter keep their rows — still quarantined under their real category, never
    deleted-and-orphaned."""
    suite, scripts = await _mini_suite(pool)
    scripts["mini-money-01"] = []
    scripts["mini-sql-01"] = []
    first = await _runner(pool, scripts).run(_config(suite, tmp_path))
    assert first.errors["n"] == 2

    suite2, scripts2 = await _mini_suite(pool)
    healed = await _runner(pool, scripts2).run(
        _config(
            suite2,
            tmp_path,
            resume_run_id=first.eval_run_id,
            retry_errors=True,
            categories=("catalog_lookup",),
        )
    )
    assert healed.categories["catalog_lookup"]["score"] == 100.0  # in-filter: healed
    # Out-of-filter: rows intact, still quarantined, category resolved suite-wide.
    assert healed.errors["question_ids"] == ["mini-money-01"]
    assert healed.errors["by_category"] == {"royalty_math": 1}
    money_rows = await pool.fetch(
        "SELECT tier FROM app.eval_results "
        "WHERE eval_run_id = $1 AND question_id = 'mini-money-01'",
        first.eval_run_id,
    )
    assert money_rows  # not deleted by the out-of-scope heal


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
