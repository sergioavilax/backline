"""Regression-gate tests (§5.4 + the Phase 5 DoD's gate-of-the-gate).

The DoD requires the gate to *demonstrably fail* when a scorer threshold is
artificially lowered: ``test_gate_fails_when_scores_artificially_lowered`` doctors a
passing run's scores downward past the drop threshold and asserts the gate trips —
plus the exact boundary (a 3.0-point drop passes, 3.1 fails)."""

import copy
import json
from pathlib import Path
from typing import Any

from evals.gate import evaluate_gate, find_entry, load_baseline, write_baseline


def _summary(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "eval_run_id": "00000000-0000-0000-0000-000000000001",
        "suite_hash": "abc123",
        "model": "claude-sonnet-5",
        "track": "platform",
        "subset": "gate",
        "git_sha": "deadbeef",
        "categories": {
            "royalty_math": {"n": 7, "score": 90.0, "tiers": {"t1": 90.0}},
            "abstention": {"n": 4, "score": 100.0, "tiers": {"t1": 100.0}},
            "adversarial": {"n": 3, "score": 100.0, "tiers": {"t2": 100.0}},
        },
        "t2_violations": 0,
        "n_questions": 14,
        "n_scored": 14,
        "n_skipped_budget": 0,
        "total_cost_usd": "1.23",
        "budget_usd": "5.00",
        "budget_exhausted": False,
        "judge": None,
        "latency_ms_p50": 900,
        "latency_ms_p95": 2100,
    }
    base.update(overrides)
    return base


def _baseline_doc(summary: dict[str, Any], tmp_path: Path) -> dict[str, Any]:
    path = tmp_path / "baseline.json"
    write_baseline(summary, path=path, note="test baseline")
    return load_baseline(path)


def test_gate_passes_against_equal_baseline(tmp_path: Path) -> None:
    summary = _summary()
    doc = _baseline_doc(summary, tmp_path)
    result = evaluate_gate(summary, doc)
    assert result.passed and not result.bootstrap and not result.reasons


def test_gate_bootstrap_passes_loudly_without_entry() -> None:
    result = evaluate_gate(_summary(), {"baselines": []})
    assert result.passed and result.bootstrap
    assert "BOOTSTRAP" in result.render()


def test_gate_fails_when_scores_artificially_lowered(tmp_path: Path) -> None:
    """The DoD check: lower a category's score past the threshold → the gate trips."""
    good = _summary()
    doc = _baseline_doc(good, tmp_path)

    lowered = copy.deepcopy(good)
    lowered["categories"]["royalty_math"]["score"] = 80.0  # -10 pts
    result = evaluate_gate(lowered, doc)
    assert not result.passed
    assert any("royalty_math" in reason and "-10.0" in reason for reason in result.reasons)
    assert "FAIL" in result.render()


def test_gate_drop_boundary_is_exactly_three_points(tmp_path: Path) -> None:
    good = _summary()
    doc = _baseline_doc(good, tmp_path)

    at_limit = copy.deepcopy(good)
    at_limit["categories"]["royalty_math"]["score"] = 87.0  # exactly -3.0
    assert evaluate_gate(at_limit, doc).passed

    past_limit = copy.deepcopy(good)
    past_limit["categories"]["royalty_math"]["score"] = 86.9  # -3.1
    assert not evaluate_gate(past_limit, doc).passed


def test_gate_fails_on_t2_violations(tmp_path: Path) -> None:
    good = _summary()
    doc = _baseline_doc(good, tmp_path)
    violating = _summary(t2_violations=2)
    result = evaluate_gate(violating, doc)
    assert not result.passed
    assert any("T2 violation" in reason for reason in result.reasons)


def test_gate_fails_on_stale_suite_hash(tmp_path: Path) -> None:
    good = _summary()
    doc = _baseline_doc(good, tmp_path)
    moved = _summary(suite_hash="fff999")
    result = evaluate_gate(moved, doc)
    assert not result.passed
    assert any("stale" in reason for reason in result.reasons)


def test_gate_fails_on_missing_category(tmp_path: Path) -> None:
    good = _summary()
    doc = _baseline_doc(good, tmp_path)
    partial = copy.deepcopy(good)
    del partial["categories"]["adversarial"]
    result = evaluate_gate(partial, doc)
    assert not result.passed
    assert any("missing" in reason for reason in result.reasons)


def test_gate_fails_on_budget_exhausted_partial_run(tmp_path: Path) -> None:
    good = _summary()
    doc = _baseline_doc(good, tmp_path)
    partial = _summary(budget_exhausted=True, n_scored=9)
    result = evaluate_gate(partial, doc)
    assert not result.passed
    assert any("partial run" in reason for reason in result.reasons)


def test_gate_fails_on_quarantined_infra_errors(tmp_path: Path) -> None:
    """D-032: a run carrying quarantined provider-outage questions is not a complete
    measurement — same footing as a budget-exhausted partial run."""
    good = _summary()
    doc = _baseline_doc(good, tmp_path)
    errored = _summary(
        errors={"n": 2, "question_ids": ["q-1", "q-2"], "by_category": {"royalty_math": 2}}
    )
    result = evaluate_gate(errored, doc)
    assert not result.passed
    assert any("infra-errored" in reason for reason in result.reasons)
    # An explicit empty bucket (a healed run) and a pre-D-032 summary both pass.
    healed = _summary(errors={"n": 0, "question_ids": [], "by_category": {}})
    assert evaluate_gate(healed, doc).passed
    assert evaluate_gate(_summary(), doc).passed


def test_gate_notes_improvements(tmp_path: Path) -> None:
    good = _summary()
    doc = _baseline_doc(good, tmp_path)
    better = copy.deepcopy(good)
    better["categories"]["royalty_math"]["score"] = 95.0
    result = evaluate_gate(better, doc)
    assert result.passed
    assert any("improved" in note for note in result.notes)


def test_write_baseline_upserts_by_key(tmp_path: Path) -> None:
    path = tmp_path / "baseline.json"
    write_baseline(_summary(), path=path, note="first")
    write_baseline(_summary(track="b0"), path=path, note="b0 run")
    updated = _summary()
    updated["categories"]["royalty_math"]["score"] = 92.0
    write_baseline(updated, path=path, note="refreshed")

    doc = load_baseline(path)
    assert len(doc["baselines"]) == 2  # platform entry replaced, b0 added
    entry = find_entry(doc, _summary())
    assert entry is not None
    assert entry["categories"]["royalty_math"] == 92.0
    assert entry["note"] == "refreshed"
    # Deterministic, committed-file-friendly serialization.
    assert json.loads(path.read_text()) == doc
