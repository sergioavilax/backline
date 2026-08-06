"""Keyless sweep-core tests: the committed matrix encodes the operator policy, the
budget math stays honest across the known price transition, and the results-doc
builder derives its metrics correctly (Phase 7)."""

from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from backline.config import get_settings
from backline.providers.registry import ModelRegistry
from benchmarks.run_sweep import parse_args, resolve_rows
from benchmarks.sweep import (
    UNCAPPED,
    RunAggregates,
    SweepMatrix,
    SweepRow,
    build_results_doc,
    completed_results,
    load_matrix,
    load_state,
    overall_score,
    save_state,
    validate_matrix_models,
    write_results_doc,
)
from evals.runner import RunnerConfig, project_cost
from evals.types import load_suite


def test_committed_matrix_encodes_the_operator_policy() -> None:
    """API rows first — opus hard-capped at $35 — local-qwen deferred to LOCAL.md."""
    matrix = load_matrix()
    assert [row.model for row in matrix.rows] == [
        "claude-opus-5",
        "claude-sonnet-5",
        "claude-haiku-4-5",
    ]
    assert matrix.rows[0].budget_usd == Decimal("35.00")  # the operator's hard budget
    assert [row.model for row in matrix.followups] == ["local-qwen"]
    assert matrix.followups[0].uncapped
    assert matrix.track == "platform"
    assert matrix.judge_model == "claude-sonnet-5"
    validate_matrix_models(matrix, ModelRegistry.load())


def test_committed_budgets_cover_projection_across_the_price_transition() -> None:
    """Every capped row must clear its pre-run projection on both sides of the
    scheduled claude-sonnet-5 price change (D-017) — otherwise the unattended sweep
    would refuse at start the day the calendar flips."""
    suite = load_suite("core")
    matrix = load_matrix()
    for on in (date(2026, 8, 15), date(2026, 9, 15)):
        registry = ModelRegistry.load(on=on)
        for row in [*matrix.rows, *matrix.followups]:
            if row.uncapped:
                continue
            config = RunnerConfig(suite=suite, model=row.model, judge_model=matrix.judge_model)
            projection = project_cost(suite.questions, config, registry)
            assert projection <= row.budget_usd, (
                f"{row.model} projects ${projection} on {on} against a "
                f"${row.budget_usd} cap — resize sweep.yaml consciously"
            )


def test_matrix_rejects_float_budgets(tmp_path: Path) -> None:
    bad = tmp_path / "sweep.yaml"
    bad.write_text(
        "suite: core\ntrack: platform\njudge_model: claude-sonnet-5\n"
        "rows:\n  - model: claude-opus-5\n    budget_usd: 35.0\n",
        encoding="utf-8",
    )
    with pytest.raises(ValidationError, match="never float"):
        load_matrix(bad)


def test_matrix_refuses_models_missing_from_the_registry() -> None:
    matrix = SweepMatrix(
        suite="core",
        track="platform",
        judge_model="claude-sonnet-5",
        rows=[SweepRow(model="claude-nonexistent-9", budget_usd=Decimal("1"))],
    )
    with pytest.raises(ValueError, match="claude-nonexistent-9"):
        validate_matrix_models(matrix, ModelRegistry.load())


def test_zero_budget_means_uncapped_at_the_runner() -> None:
    """The runner's stop gate is ``spent + reserved >= budget`` — a literal $0 cap
    would skip every question, so the zero-priced local row maps to Infinity."""
    local = SweepRow(model="local-qwen", budget_usd=Decimal("0"))
    assert local.uncapped
    assert local.runner_budget == UNCAPPED
    assert Decimal("0") < UNCAPPED  # the gate comparison stays false forever
    capped = SweepRow(model="claude-opus-5", budget_usd=Decimal("35.00"))
    assert not capped.uncapped
    assert capped.runner_budget == Decimal("35.00")


def test_state_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    assert load_state(path) == {}
    state = {"claude-opus-5": {"eval_run_id": "abc", "suite_hash": "h", "track": "platform"}}
    save_state(path, state)
    assert load_state(path) == state
    state.pop("claude-opus-5")
    save_state(path, state)
    assert load_state(path) == {}


def _summary(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "eval_run_id": "00000000-0000-0000-0000-000000000001",
        "suite_hash": "6eef41c6706f309a",
        "model": "claude-haiku-4-5",
        "track": "platform",
        "subset": None,
        "git_sha": "abcdef123456",
        "categories": {
            "catalog_lookup": {"n": 15, "score": 100.0, "tiers": {"t1": 100.0}},
            "royalty_math": {"n": 25, "score": 80.0, "tiers": {"t1": 80.0}},
        },
        "t2_violations": 0,
        "n_questions": 40,
        "n_scored": 40,
        "n_skipped_budget": 0,
        "total_cost_usd": "5.00",
        "budget_usd": "9.00",
        "budget_exhausted": False,
        "judge": {"model": "claude-sonnet-5", "rubric_sha256": "cafe"},
        "latency_ms_p50": 8000,
        "latency_ms_p95": 41000,
    }
    base.update(overrides)
    return base


def _aggregates() -> RunAggregates:
    return RunAggregates(
        runs=40,
        runs_completed=36,
        runs_exhausted=3,
        runs_error=1,
        agent_cost_usd=Decimal("4.20"),
        iterations=170,
        tool_calls=200,
        tool_calls_by_status={"denied": 2, "error": 5, "ok": 190, "timeout": 3},
        tokens_in=900_000,
        tokens_out=120_000,
    )


def test_build_results_doc_derives_the_benchmark_metrics() -> None:
    registry = ModelRegistry.load()
    row = SweepRow(model="claude-haiku-4-5", budget_usd=Decimal("9.00"))
    doc = build_results_doc(
        _summary(),
        _aggregates(),
        registry=registry,
        row=row,
        suite_name="core",
        settings=get_settings(),
        concurrency=4,
        recorded_at="2026-08-06",
    )
    # $/query is the agent loop alone; judge spend is the difference, kept visible.
    assert doc["usd_per_query"] == "0.1050"  # 4.20 / 40
    assert doc["usd_per_query_with_judge"] == "0.1250"  # 5.00 / 40
    assert doc["agent_cost_usd"] == "4.20"
    assert doc["judge_cost_usd"] == "0.80"
    assert doc["iterations_mean"] == 4.25  # 170 / 40
    assert doc["tool_calls"] == {
        "n": 200,
        "errors": 10,
        "error_rate": 0.05,
        "by_status": {"denied": 2, "error": 5, "ok": 190, "timeout": 3},
    }
    assert doc["runs"] == {"n": 40, "completed": 36, "exhausted": 3, "error": 1}
    assert doc["overall_score"] == 87.5  # (15*100 + 25*80) / 40
    assert doc["complete"] is True
    assert doc["provider"] == "anthropic"
    assert doc["budget_usd"] == "9.00"
    assert doc["tokens"] == {"input": 900_000, "output": 120_000}
    assert doc["recorded_at"] == "2026-08-06"


def test_build_results_doc_marks_partials_and_uncapped_rows() -> None:
    registry = ModelRegistry.load()
    partial = build_results_doc(
        _summary(n_scored=25, n_skipped_budget=15, budget_exhausted=True),
        _aggregates(),
        registry=registry,
        row=SweepRow(model="claude-haiku-4-5", budget_usd=Decimal("9.00")),
        suite_name="core",
        settings=get_settings(),
        concurrency=4,
        recorded_at="2026-08-06",
    )
    assert partial["complete"] is False
    assert partial["n_skipped_budget"] == 15

    local = build_results_doc(
        _summary(model="local-qwen", total_cost_usd="0.34"),
        _aggregates(),
        registry=registry,
        row=SweepRow(model="local-qwen", budget_usd=Decimal("0")),
        suite_name="core",
        settings=get_settings(),
        concurrency=4,
        recorded_at="2026-08-06",
    )
    assert local["budget_usd"] == "0 (uncapped)"
    assert local["provider"] == "openai_compat"


def test_overall_score_is_question_weighted() -> None:
    assert overall_score({"a": {"n": 1, "score": 100.0}, "b": {"n": 3, "score": 0.0}}) == 25.0
    assert overall_score({}) == 0.0


def test_completed_results_requires_completeness_and_suite_match(tmp_path: Path) -> None:
    registry = ModelRegistry.load()
    doc = build_results_doc(
        _summary(),
        _aggregates(),
        registry=registry,
        row=SweepRow(model="claude-haiku-4-5", budget_usd=Decimal("9.00")),
        suite_name="core",
        settings=get_settings(),
        concurrency=4,
        recorded_at="2026-08-06",
    )
    write_results_doc(tmp_path, doc)
    assert completed_results(tmp_path, "claude-haiku-4-5", "6eef41c6706f309a") is not None
    assert completed_results(tmp_path, "claude-haiku-4-5", "someothersuite00") is None
    assert completed_results(tmp_path, "claude-opus-5", "6eef41c6706f309a") is None


# ── CLI row resolution ───────────────────────────────────────────────────────────


def test_default_invocation_runs_the_api_rows_in_matrix_order() -> None:
    matrix = load_matrix()
    args = parse_args([])
    rows = resolve_rows(args, matrix.rows, matrix.find)
    assert [row.model for row in rows] == [
        "claude-opus-5",
        "claude-sonnet-5",
        "claude-haiku-4-5",
    ]


def test_single_model_resolves_followups_and_budget_overrides() -> None:
    matrix = load_matrix()
    args = parse_args(["--model", "local-qwen"])
    (row,) = resolve_rows(args, matrix.rows, matrix.find)
    assert row.model == "local-qwen" and row.uncapped

    args = parse_args(["--model", "claude-opus-5", "--budget", "12.50"])
    (row,) = resolve_rows(args, matrix.rows, matrix.find)
    assert row.budget_usd == Decimal("12.50")

    args = parse_args(["--model", "claude-mystery-7"])
    with pytest.raises(SystemExit, match="not in the sweep matrix"):
        resolve_rows(args, matrix.rows, matrix.find)
    args = parse_args(["--model", "claude-mystery-7", "--budget", "1.00"])
    (row,) = resolve_rows(args, matrix.rows, matrix.find)
    assert row.model == "claude-mystery-7" and row.budget_usd == Decimal("1.00")


def test_budget_and_resume_require_model() -> None:
    with pytest.raises(SystemExit):
        parse_args(["--budget", "5.00"])
    with pytest.raises(SystemExit):
        parse_args(["--resume", "00000000-0000-0000-0000-000000000001"])
    with pytest.raises(SystemExit):
        parse_args(["--judge", "claude-sonnet-5", "--no-judge"])
