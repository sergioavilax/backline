"""Keyless report tests: the sweep report renders whatever results exist —
API-only, partial, or with the local row — and the comparison chart stays
deterministic and theme-adaptive (Phase 7: "the report must degrade gracefully
to API-only")."""

from pathlib import Path
from typing import Any

from backline.jsonutil import canonical_dumps
from benchmarks.report import (
    load_row_results,
    render_comparison_svg,
    render_report_md,
    write_report,
)
from benchmarks.sweep import load_matrix


def _doc(model: str, **overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "model": model,
        "provider": "anthropic",
        "price": {"usd_per_mtok_in": "2.00", "usd_per_mtok_out": "10.00", "note": ""},
        "suite": "core",
        "suite_hash": "6eef41c6706f309a",
        "track": "platform",
        "subset": None,
        "eval_run_id": "11111111-2222-3333-4444-555555555555",
        "git_sha": "abcdef123456",
        "recorded_at": "2026-08-06",
        "judge": {"model": "claude-sonnet-5", "rubric_sha256": "cafe"},
        "n_questions": 133,
        "n_scored": 133,
        "n_skipped_budget": 0,
        "budget_usd": "20.00",
        "budget_exhausted": False,
        "complete": True,
        "categories": {
            "catalog_lookup": {"n": 15, "score": 100.0, "tiers": {"t1": 100.0}},
            "royalty_math": {"n": 25, "score": 92.0, "tiers": {"t1": 92.0}},
        },
        "overall_score": 95.0,
        "total_cost_usd": "11.90",
        "agent_cost_usd": "10.90",
        "judge_cost_usd": "1.00",
        "usd_per_query": "0.0820",
        "usd_per_query_with_judge": "0.0895",
        "latency_ms_p50": 9200,
        "latency_ms_p95": 61000,
        "iterations_mean": 4.4,
        "runs": {"n": 133, "completed": 130, "exhausted": 3, "error": 0},
        "tool_calls": {"n": 790, "errors": 25, "error_rate": 0.0316, "by_status": {}},
        "tokens": {"input": 900_000, "output": 180_000},
        "t2_violations": 0,
        "runtime_config": {
            "utility_model": "claude-haiku-4-5",
            "run_budget_usd": "0.50",
            "max_iterations": 12,
            "concurrency": 4,
        },
    }
    base.update(overrides)
    return base


def test_report_degrades_gracefully_to_api_only() -> None:
    """The §7 contract: three API rows render, the local row shows as a pending
    follow-up pointing at LOCAL.md — never an error, never an empty table."""
    matrix = load_matrix()
    docs = [
        _doc("claude-opus-5", usd_per_query="0.1893", overall_score=92.4),
        _doc("claude-sonnet-5"),
        _doc("claude-haiku-4-5", usd_per_query="0.0340", overall_score=84.0),
    ]
    text = render_report_md(matrix, docs, ["local-qwen"])
    assert "| claude-opus-5 |" in text
    assert "| claude-sonnet-5 |" in text
    assert "| claude-haiku-4-5 |" in text
    assert "*pending:* `local-qwen` — follow-up row, run per `benchmarks/LOCAL.md`" in text
    assert "$0.1893" in text and "$0.0340" in text
    assert "9.2s" in text and "61.0s" in text  # latency renders in seconds
    assert "†" not in text  # no partials, no footnote noise


def test_report_with_no_results_says_what_to_run() -> None:
    matrix = load_matrix()
    text = render_report_md(matrix, [], matrix.model_ids)
    assert "No results yet" in text
    assert "run_sweep.py" in text and "LOCAL.md" in text
    for model in matrix.model_ids:
        assert f"`{model}`" in text


def test_partial_rows_carry_the_dagger_and_resume_hint() -> None:
    matrix = load_matrix()
    docs = [
        _doc("claude-opus-5"),
        _doc(
            "claude-haiku-4-5",
            complete=False,
            n_scored=97,
            n_skipped_budget=36,
            budget_exhausted=True,
        ),
    ]
    text = render_report_md(matrix, docs, [])
    assert "| claude-haiku-4-5 † |" in text
    assert "97/133 †" in text
    assert "--resume" in text
    # Category columns inherit the marker so partial scores are never mistaken
    # for final ones.
    assert "| category | claude-opus-5 | claude-haiku-4-5 † |" in text


def test_errored_rows_carry_the_double_dagger_and_heal_command() -> None:
    """D-032: an infra-errored row is visibly quarantined — ‡ on the row label, the
    scored cell, the affected category cells — with the exact heal command in the
    footnote. The outage never reads as model incapability."""
    matrix = load_matrix()
    docs = [
        _doc("claude-sonnet-5"),
        _doc(
            "claude-opus-5",
            complete=False,
            categories={
                "catalog_lookup": {"n": 15, "score": 100.0, "tiers": {"t1": 100.0}},
                "reconciliation": {"n": 5, "score": 62.0, "tiers": {"t1": 62.0}},
            },
            errors={
                "n": 10,
                "question_ids": [f"recon-{i:02d}" for i in range(10)],
                "by_category": {"reconciliation": 10},
            },
        ),
    ]
    text = render_report_md(matrix, docs, [])
    assert "| claude-opus-5 ‡ |" in text
    assert "123/133 ‡" in text  # 133 rows minus 10 quarantined
    assert "62.0 ‡" in text  # the surviving reconciliation bucket stays marked
    assert "‡ 10 infra-errored (quarantined)" in text  # row provenance
    assert "--retry-errors" in text
    assert "† partial run" not in text  # errored ≠ budget-partial

    # A row can be both budget-stopped and errored; both footnotes render.
    both = _doc(
        "claude-haiku-4-5",
        complete=False,
        n_scored=97,
        budget_exhausted=True,
        errors={"n": 2, "question_ids": ["a", "b"], "by_category": {"abstention": 2}},
    )
    text = render_report_md(matrix, [both], [])
    assert "| claude-haiku-4-5 †‡ |" in text
    assert "95/133 †‡" in text
    assert "† partial run" in text and "‡ infra-errored" in text


def test_errored_rows_are_excluded_from_the_chart() -> None:
    docs = [
        _doc("claude-sonnet-5"),
        _doc(
            "claude-opus-5",
            complete=False,
            errors={"n": 10, "question_ids": ["q"], "by_category": {"reconciliation": 10}},
        ),
    ]
    svg = render_comparison_svg(docs)
    assert svg is not None
    assert "claude-sonnet-5" in svg
    assert "partial rows excluded: claude-opus-5" in svg


def test_local_row_joins_the_table_once_its_results_land() -> None:
    matrix = load_matrix()
    docs = [
        _doc("claude-opus-5"),
        _doc(
            "local-qwen",
            provider="openai_compat",
            price={"usd_per_mtok_in": "0", "usd_per_mtok_out": "0", "note": ""},
            usd_per_query="0.0000",
            total_cost_usd="0.34",
            agent_cost_usd="0",
            judge_cost_usd="0.34",
            budget_usd="0 (uncapped)",
        ),
    ]
    text = render_report_md(matrix, docs, [])
    assert "| local-qwen |" in text
    assert "pending" not in text


def test_category_matrix_uses_canonical_order_with_gaps_dashed() -> None:
    matrix = load_matrix()
    docs = [
        _doc("claude-opus-5"),
        _doc(
            "claude-sonnet-5",
            categories={"royalty_math": {"n": 25, "score": 88.0, "tiers": {}}},
        ),
    ]
    text = render_report_md(matrix, docs, [])
    lines = text.splitlines()
    catalog = next(line for line in lines if line.startswith("| catalog_lookup"))
    royalty = next(line for line in lines if line.startswith("| royalty_math"))
    assert catalog == "| catalog_lookup | 100.0 | — |"
    assert royalty == "| royalty_math | 92.0 | 88.0 |"
    assert lines.index(catalog) < lines.index(royalty)  # §5.2 category order


def test_svg_plots_complete_rows_and_is_deterministic() -> None:
    docs = [
        _doc("claude-opus-5", usd_per_query="0.1893", overall_score=92.4),
        _doc("claude-sonnet-5", usd_per_query="0.0820", overall_score=90.1),
        _doc("claude-haiku-4-5", complete=False, usd_per_query="0.0340"),
    ]
    svg = render_comparison_svg(docs)
    assert svg is not None
    assert svg == render_comparison_svg(docs)  # byte-stable across renders
    assert svg.startswith("<svg ") and svg.rstrip().endswith("</svg>")
    assert svg.count("<circle") == 2  # complete rows only
    assert "claude-opus-5" in svg and "claude-sonnet-5" in svg
    assert "partial rows excluded: claude-haiku-4-5" in svg
    # Theme-adaptive: dark mode is selected via the media query, not flipped.
    assert "@media (prefers-color-scheme: dark)" in svg
    assert "#2a78d6" in svg and "#3987e5" in svg  # validated slot-1 hue, both modes


def test_svg_absent_until_a_complete_row_exists() -> None:
    assert render_comparison_svg([]) is None
    assert render_comparison_svg([_doc("claude-opus-5", complete=False)]) is None


def test_write_report_emits_files_and_off_matrix_results_still_show(
    tmp_path: Path,
) -> None:
    matrix = load_matrix()
    results = tmp_path / "results"
    results.mkdir()
    for doc in (_doc("claude-sonnet-5"), _doc("claude-experimental-x")):
        (results / f"{doc['model']}.json").write_text(canonical_dumps(doc) + "\n", encoding="utf-8")
    docs, pending = load_row_results(matrix, results)
    assert [d["model"] for d in docs] == ["claude-sonnet-5", "claude-experimental-x"]
    assert pending == ["claude-opus-5", "claude-haiku-4-5", "local-qwen"]

    report_path = write_report(matrix, results)
    assert report_path == results / "REPORT.md"
    text = report_path.read_text(encoding="utf-8")
    assert "claude-experimental-x" in text
    assert (results / "comparison.svg").exists()


def test_write_report_with_empty_results_dir_writes_pending_report(tmp_path: Path) -> None:
    matrix = load_matrix()
    results = tmp_path / "results"
    report_path = write_report(matrix, results)
    assert "No results yet" in report_path.read_text(encoding="utf-8")
    assert not (results / "comparison.svg").exists()
