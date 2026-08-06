"""T1 scoring (BUILD_PLAN §5.1): deterministic answer checks against the answer key.

Numeric answers score within tolerance (±$0.01 on money unless the question says
otherwise; exact on counts); abstention questions score the *typed* abstention flag —
the finalizer's ``ABSTAIN:`` protocol, not prose sentiment. Reconciliation scores as
flag precision/recall/F1 against the registry, with the borderline non-flags reported
explicitly (flagging a within-tolerance measurement is a precision failure, §3.4).

Every result carries a ``detail`` dict rich enough for the report's drill-down:
expected vs extracted, the failure mode (``no_answer_line``, ``unparseable``,
``abstained_unexpectedly``...), and for flags the exact hit/miss/extra sets.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from evals.answers import (
    extract_answer,
    extract_flags,
    normalize_value,
    parse_bool,
    parse_count,
    parse_money,
    parse_percent,
    parse_period,
    parse_set,
)
from evals.types import Question


@dataclass(frozen=True)
class AnswerOutcome:
    """What a track produced for one question — the scorer's only input surface."""

    text: str
    abstained: bool = False
    citations: tuple[str, ...] = ()
    batch_id: int | None = None
    status: str = "completed"


@dataclass(frozen=True)
class TierScore:
    score: float  # 0..1
    passed: bool
    detail: dict[str, Any] = field(default_factory=dict)


def _fail(reason: str, **extra: Any) -> TierScore:
    return TierScore(score=0.0, passed=False, detail={"failure": reason, **extra})


def _scalar(question: Question, outcome: AnswerOutcome) -> TierScore:
    raw = extract_answer(outcome.text)
    if raw is None:
        return _fail("no_answer_line")
    expected = question.expected
    detail: dict[str, Any] = {"expected": expected, "extracted": raw}

    kind = question.answer_kind
    if kind == "money":
        actual = parse_money(raw)
        if actual is None:
            return _fail("unparseable_money", **detail)
        tolerance = Decimal(question.tolerance or "0.01")
        delta = abs(actual - Decimal(str(expected)))
        passed = delta <= tolerance
        return TierScore(
            score=1.0 if passed else 0.0,
            passed=passed,
            detail={**detail, "delta": str(delta), "tolerance": str(tolerance)},
        )
    if kind == "count":
        actual_count = parse_count(raw)
        if actual_count is None:
            return _fail("unparseable_count", **detail)
        passed = actual_count == int(str(expected))
        return TierScore(1.0 if passed else 0.0, passed, detail)
    if kind == "percent":
        actual_pct = parse_percent(raw)
        if actual_pct is None:
            return _fail("unparseable_percent", **detail)
        passed = actual_pct == Decimal(str(expected)).normalize()
        return TierScore(1.0 if passed else 0.0, passed, detail)
    if kind == "bool":
        actual_bool = parse_bool(raw)
        if actual_bool is None:
            return _fail("unparseable_bool", **detail)
        passed = actual_bool == str(expected)
        return TierScore(1.0 if passed else 0.0, passed, detail)
    if kind == "period":
        actual_period = parse_period(raw)
        if actual_period is None:
            return _fail("unparseable_period", **detail)
        passed = actual_period == str(expected)
        return TierScore(1.0 if passed else 0.0, passed, detail)
    if kind == "set":
        actual_set = parse_set(raw)
        expected_set = {normalize_value(item) for item in expected}
        hit = actual_set & expected_set
        jaccard = len(hit) / len(actual_set | expected_set) if (actual_set | expected_set) else 1.0
        passed = actual_set == expected_set
        return TierScore(
            score=1.0 if passed else 0.0,
            passed=passed,
            detail={
                **detail,
                "missing": sorted(expected_set - actual_set),
                "extra": sorted(actual_set - expected_set),
                "jaccard": round(jaccard, 4),
            },
        )
    # kind == "value"
    passed = normalize_value(raw) == normalize_value(str(expected))
    return TierScore(1.0 if passed else 0.0, passed, detail)


def _abstain(question: Question, outcome: AnswerOutcome) -> TierScore:
    """§5.1: abstention questions score exact ABSTAIN — the typed flag."""
    if outcome.abstained:
        return TierScore(1.0, True, {"abstained": True})
    return _fail("did_not_abstain", answered=extract_answer(outcome.text) or "")


def _flags(question: Question, outcome: AnswerOutcome) -> TierScore:
    """Reconciliation: precision/recall/F1 over (kind, source, line_id) triples.

    Borderline line ids are scored-by-absence: they are not in the expected set, so
    flagging one costs precision — and the detail names it, because "flagged a
    within-tolerance measurement" is the §3.4 trap this category exists to catch.
    """
    expected_entries = {
        (f["kind"], f["source"], int(f["line_id"])) for f in question.expected["flags"]
    }
    borderline_ids = set(question.expected.get("borderline_line_ids", []))
    actual = extract_flags(outcome.text)

    hits = actual & expected_entries
    precision = len(hits) / len(actual) if actual else (1.0 if not expected_entries else 0.0)
    recall = len(hits) / len(expected_entries) if expected_entries else 1.0
    if not expected_entries and not actual:
        precision = recall = 1.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0.0
    borderline_flagged = sorted({line for _, _, line in actual} & borderline_ids)
    passed = f1 == 1.0 and not borderline_flagged
    return TierScore(
        score=round(f1, 4),
        passed=passed,
        detail={
            "precision": round(precision, 4),
            "recall": round(recall, 4),
            "expected_n": len(expected_entries),
            "flagged_n": len(actual),
            "missing": sorted(expected_entries - actual),
            "extra": sorted(actual - expected_entries),
            "borderline_flagged": borderline_flagged,
        },
    )


def score_t1(question: Question, outcome: AnswerOutcome) -> TierScore:
    if outcome.status != "completed":
        return _fail(f"run_{outcome.status}")
    if question.answer_kind == "abstain":
        return _abstain(question, outcome)
    if outcome.abstained:
        return _fail("abstained_unexpectedly")
    if question.answer_kind == "flags":
        return _flags(question, outcome)
    return _scalar(question, outcome)
