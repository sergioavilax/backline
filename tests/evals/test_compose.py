"""Composite-summary tests (D-023): merging targeted category re-runs into one
gate-ready baseline summary, and every way the merge must refuse.

The composite exists for the post-diagnosis close-out shape: after a diagnose →
fix → re-run cycle, the latest valid measurement of each category lives in
different runs of the *same* committed suite. Composing them must be exactly as
strict as a real, complete run — same (model, track, subset, suite_hash), the
committed suite's hash, full per-category question counts, exact coverage."""

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from evals.compose import ComposeError, compose_summaries, provenance_note, render_composite
from evals.gate import evaluate_gate, find_entry, load_baseline, write_baseline
from evals.types import Question, Suite, dump_suite, suite_hash


def _question(qid: str, category: str, *, in_gate: bool = False) -> Question:
    return Question.model_validate(
        {
            "id": qid,
            "category": category,
            "agent": "counsel",
            "tiers": ["t1"],
            "prompt": f"What is {qid}? Answer with `ANSWER: <value>`.",
            "answer_kind": "value",
            "expected": "x",
            "in_gate": in_gate,
        }
    )


_QUESTIONS = [
    _question("royalty_math-001", "royalty_math", in_gate=True),
    _question("royalty_math-002", "royalty_math"),
    _question("abstention-001", "abstention", in_gate=True),
    _question("multi_step-001", "multi_step"),
]

SUITE = Suite(
    name="tiny",
    world_seed=1,
    suite_hash=suite_hash(_QUESTIONS),
    questions=_QUESTIONS,
)


def _bucket(n: int, score: float) -> dict[str, Any]:
    return {"n": n, "score": score, "tiers": {"t1": score}}


def _summary(
    *,
    run_id: str,
    sha: str | None,
    categories: dict[str, dict[str, Any]],
    subset: str | None = None,
    model: str = "claude-sonnet-5",
    track: str = "platform",
    hash_value: str = SUITE.suite_hash,
) -> dict[str, Any]:
    n = sum(bucket["n"] for bucket in categories.values())
    return {
        "eval_run_id": run_id,
        "suite_hash": hash_value,
        "model": model,
        "track": track,
        "subset": subset,
        "git_sha": sha,
        "categories": categories,
        "t2_violations": 0,
        "n_questions": n,
        "n_scored": n,
        "n_skipped_budget": 0,
        "total_cost_usd": "1.00",
        "budget_usd": "14.00",
        "budget_exhausted": False,
        "judge": None,
        "latency_ms_p50": 900,
        "latency_ms_p95": 2100,
    }


def _full(
    run_id: str = "aaaa1111-0000-0000-0000-000000000000", sha: str = "sha1full0000"
) -> dict[str, Any]:
    return _summary(
        run_id=run_id,
        sha=sha,
        categories={
            "royalty_math": _bucket(2, 90.0),
            "abstention": _bucket(1, 25.0),
            "multi_step": _bucket(1, 10.0),
        },
    )


def _redo() -> dict[str, Any]:
    """The targeted re-run: abstention fixed, multi_step still broken."""
    return _summary(
        run_id="bbbb2222-0000-0000-0000-000000000000",
        sha="sha2redo0000",
        categories={"abstention": _bucket(1, 100.0), "multi_step": _bucket(1, 40.0)},
    )


def _final() -> dict[str, Any]:
    return _summary(
        run_id="cccc3333-0000-0000-0000-000000000000",
        sha="sha3final000",
        categories={"multi_step": _bucket(1, 72.8)},
    )


def test_later_summaries_override_per_category() -> None:
    composed = compose_summaries([_full(), _redo(), _final()], SUITE)

    scores = {cat: bucket["score"] for cat, bucket in composed["categories"].items()}
    assert scores == {"royalty_math": 90.0, "abstention": 100.0, "multi_step": 72.8}
    assert composed["model"] == "claude-sonnet-5"
    assert composed["track"] == "platform"
    assert composed["subset"] is None  # full-suite shape, like the runner emits
    assert composed["suite_hash"] == SUITE.suite_hash
    # The entry's git sha is the newest component's — the code state the
    # composite is asserted valid for.
    assert composed["git_sha"] == "sha3final000"
    assert composed["n_questions"] == composed["n_scored"] == 4
    # Per-category provenance: which run each score came from.
    assert composed["sources"]["royalty_math"]["eval_run_id"].startswith("aaaa1111")
    assert composed["sources"]["abstention"]["eval_run_id"].startswith("bbbb2222")
    assert composed["sources"]["multi_step"]["eval_run_id"].startswith("cccc3333")


def test_provenance_note_groups_categories_by_source_run() -> None:
    composed = compose_summaries([_full(), _redo(), _final()], SUITE)
    note = provenance_note(composed)
    assert note.startswith("composed from ")
    assert "aaaa1111@sha1full0000: royalty_math" in note
    assert "bbbb2222@sha2redo0000: abstention" in note
    assert "cccc3333@sha3final000: multi_step" in note


def test_render_composite_shows_scores_and_sources() -> None:
    text = render_composite(compose_summaries([_full(), _redo(), _final()], SUITE))
    assert "| multi_step | 1 | 72.8 | cccc3333@sha3final000 |" in text
    assert "**overall**" in text


def test_refuses_mixed_identity() -> None:
    for field, override in [
        ("model", {"model": "claude-opus-5"}),
        ("track", {"track": "b0"}),
        ("subset", {"subset": "gate"}),
        ("suite_hash", {"hash_value": "f00d f00d".replace(" ", "")}),
    ]:
        drifted = _summary(
            run_id="dddd4444-0000-0000-0000-000000000000",
            sha="sha4",
            categories={"multi_step": _bucket(1, 50.0)},
            **override,
        )
        with pytest.raises(ComposeError, match=field):
            compose_summaries([_full(), drifted], SUITE)


def test_refuses_components_from_a_different_committed_suite() -> None:
    moved = [
        _summary(
            run_id="aaaa1111-0000-0000-0000-000000000000",
            sha="sha1",
            hash_value="0123456789abcdef",
            categories={
                "royalty_math": _bucket(2, 90.0),
                "abstention": _bucket(1, 100.0),
                "multi_step": _bucket(1, 70.0),
            },
        )
    ]
    with pytest.raises(ComposeError, match="committed suite"):
        compose_summaries(moved, SUITE)


def test_refuses_incomplete_coverage() -> None:
    with pytest.raises(ComposeError, match="royalty_math"):
        compose_summaries([_redo()], SUITE)
    # The error names everything missing (sorted), not what is present.
    with pytest.raises(ComposeError, match="abstention, royalty_math"):
        compose_summaries([_final()], SUITE)


def test_refuses_partial_category_bucket() -> None:
    partial = _full()
    partial["categories"]["royalty_math"] = _bucket(1, 100.0)  # suite has 2
    with pytest.raises(ComposeError, match=r"royalty_math.*1/2"):
        compose_summaries([partial], SUITE)


def test_refuses_category_outside_the_suite() -> None:
    stray = _full()
    stray["categories"]["adversarial"] = _bucket(3, 100.0)
    with pytest.raises(ComposeError, match="adversarial"):
        compose_summaries([stray], SUITE)


def test_gate_subset_composites_use_subset_counts() -> None:
    gate_run = _summary(
        run_id="eeee5555-0000-0000-0000-000000000000",
        sha="sha5",
        subset="gate",
        categories={"royalty_math": _bucket(1, 95.0), "abstention": _bucket(1, 100.0)},
    )
    composed = compose_summaries([gate_run], SUITE)
    assert composed["subset"] == "gate"
    assert set(composed["categories"]) == {"royalty_math", "abstention"}

    # A full-suite count is *wrong* for the gate subset — refuse, don't accept more data.
    oversized = copy.deepcopy(gate_run)
    oversized["categories"]["royalty_math"] = _bucket(2, 95.0)
    with pytest.raises(ComposeError, match=r"royalty_math.*2/1"):
        compose_summaries([oversized], SUITE)


def test_single_complete_summary_composes_as_validation() -> None:
    composed = compose_summaries([_full()], SUITE)
    assert {cat: b["score"] for cat, b in composed["categories"].items()} == {
        "royalty_math": 90.0,
        "abstention": 25.0,
        "multi_step": 10.0,
    }


def test_composed_baseline_feeds_the_gate(tmp_path: Path) -> None:
    """The point of the exercise: write_baseline(composed) yields an entry the
    gate compares future full runs against — passing at parity, tripping on a drop."""
    composed = compose_summaries([_full(), _redo(), _final()], SUITE)
    path = tmp_path / "baseline.json"
    write_baseline(composed, path=path, note=provenance_note(composed))

    doc = load_baseline(path)
    entry = find_entry(doc, composed)
    assert entry is not None
    assert entry["subset"] == "full"
    assert entry["git_sha"] == "sha3final000"
    assert entry["categories"] == {"royalty_math": 90.0, "abstention": 100.0, "multi_step": 72.8}
    assert "cccc3333@sha3final000: multi_step" in entry["note"]

    future = _summary(
        run_id="ffff6666-0000-0000-0000-000000000000",
        sha="sha6future00",
        categories={
            "royalty_math": _bucket(2, 91.0),
            "abstention": _bucket(1, 100.0),
            "multi_step": _bucket(1, 74.0),
        },
    )
    assert evaluate_gate(future, doc).passed

    regressed = copy.deepcopy(future)
    regressed["categories"]["abstention"] = _bucket(1, 80.0)
    result = evaluate_gate(regressed, doc)
    assert not result.passed
    assert any("abstention" in reason for reason in result.reasons)


def test_cli_compose_writes_baseline(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from evals.__main__ import main

    suite_path = tmp_path / "tiny.json"
    suite_path.write_text(dump_suite(SUITE), encoding="utf-8")
    paths = []
    for name, summary in [("full", _full()), ("redo", _redo()), ("final", _final())]:
        p = tmp_path / f"{name}.json"
        p.write_text(json.dumps(summary), encoding="utf-8")
        paths.append(str(p))
    baseline_path = tmp_path / "baseline.json"

    code = main(
        [
            "compose",
            "--summary",
            *paths,
            "--suite",
            str(suite_path),
            "--baseline",
            str(baseline_path),
            "--write-baseline",
            "--note",
            "close-out",
        ]
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "composed from" in out and "baseline updated" in out

    entry = load_baseline(baseline_path)["baselines"][0]
    assert entry["categories"]["multi_step"] == 72.8
    assert entry["note"].startswith("close-out · composed from ")


def test_cli_compose_dry_run_and_refusal(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from evals.__main__ import main

    suite_path = tmp_path / "tiny.json"
    suite_path.write_text(dump_suite(SUITE), encoding="utf-8")
    full = tmp_path / "full.json"
    full.write_text(json.dumps(_full()), encoding="utf-8")
    baseline_path = tmp_path / "baseline.json"

    code = main(
        [
            "compose",
            "--summary",
            str(full),
            "--suite",
            str(suite_path),
            "--baseline",
            str(baseline_path),
        ]
    )
    assert code == 0
    assert "dry run" in capsys.readouterr().out
    assert not baseline_path.exists()

    incomplete = tmp_path / "incomplete.json"
    incomplete.write_text(json.dumps(_redo()), encoding="utf-8")
    code = main(
        [
            "compose",
            "--summary",
            str(incomplete),
            "--suite",
            str(suite_path),
            "--baseline",
            str(baseline_path),
            "--write-baseline",
        ]
    )
    assert code == 2
    assert "compose refused" in capsys.readouterr().err
    assert not baseline_path.exists()
