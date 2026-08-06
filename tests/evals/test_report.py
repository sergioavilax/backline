"""Report rendering tests: stable markdown shape for README/PHASE_LOG export."""

from typing import Any


def _summary(track: str, score: float) -> dict[str, Any]:
    return {
        "eval_run_id": "00000000-0000-0000-0000-000000000001",
        "suite_hash": "abc123",
        "model": "mock-sonnet",
        "track": track,
        "subset": "smoke",
        "git_sha": "cafe12",
        "categories": {
            "royalty_math": {"n": 2, "score": score, "tiers": {"t1": score, "t2": 100.0}},
            "abstention": {"n": 1, "score": 100.0, "tiers": {"t1": 100.0}},
        },
        "t2_violations": 0,
        "n_questions": 3,
        "n_scored": 3,
        "n_skipped_budget": 0,
        "total_cost_usd": "0.02",
        "budget_usd": "1.00",
        "budget_exhausted": False,
        "judge": {"model": "mock-sonnet", "rubric_sha256": "beefbeefbeef"},
        "latency_ms_p50": 12,
        "latency_ms_p95": 40,
    }


def test_render_markdown_single_run() -> None:
    from evals.report import render_markdown

    text = render_markdown(_summary("platform", 90.0))
    assert "| royalty_math | 2 | 90.0 |" in text
    assert "| abstention | 1 | 100.0 |" in text
    assert "**93.3**" in text  # (90*2 + 100*1) / 3
    assert "T2 violations: 0" in text
    assert "judge: mock-sonnet" in text
    assert "suite `abc123`" in text


def test_render_compare_lines_up_tracks() -> None:
    from evals.report import render_compare

    text = render_compare([_summary("b0", 10.0), _summary("platform", 95.0)])
    assert "| royalty_math | 10.0 | 95.0 |" in text
    assert "b0/mock-sonnet (smoke)" in text and "platform/mock-sonnet (smoke)" in text
    assert "| **overall** |" in text
