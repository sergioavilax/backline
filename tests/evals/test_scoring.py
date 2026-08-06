"""T1 scorer + extraction tests: tolerance boundaries, protocol violations, the
reconciliation precision/recall math, and the borderline non-flag trap."""

from decimal import Decimal

from evals.answers import (
    extract_answer,
    extract_flags,
    parse_bool,
    parse_count,
    parse_money,
    parse_percent,
    parse_period,
    parse_set,
)
from evals.scoring import AnswerOutcome, score_t1
from evals.types import Question


def _q(kind: str, expected: object, tolerance: str | None = None, **meta: object) -> Question:
    return Question(
        id=f"test-{kind}",
        category="royalty_math",
        agent="counsel",
        tiers=["t1"],
        prompt="test",
        answer_kind=kind,  # type: ignore[arg-type]
        expected=expected,
        tolerance=tolerance,
        meta=dict(meta),
    )


def _out(text: str, **kwargs: object) -> AnswerOutcome:
    return AnswerOutcome(text=text, **kwargs)  # type: ignore[arg-type]


# ── extraction ───────────────────────────────────────────────────────────────


def test_extract_answer_takes_last_line() -> None:
    text = "Working...\nANSWER: $10.00\nWait, correcting.\nANSWER: $12.00"
    assert extract_answer(text) == "$12.00"


def test_extract_answer_missing() -> None:
    assert extract_answer("The payable is $12.") is None


def test_money_parsing_forms() -> None:
    assert parse_money("$1,234.56") == Decimal("1234.56")
    assert parse_money("1234.567890 USD") == Decimal("1234.567890")
    assert parse_money("-12.00") == Decimal("-12.00")
    assert parse_money("(45.10)") == Decimal("-45.10")
    assert parse_money("about $0.00") == Decimal("0.00")
    assert parse_money("no number here") is None


def test_percent_parsing_normalizes_rate_form() -> None:
    assert parse_percent("30%") == Decimal("30")
    assert parse_percent("30") == Decimal("30")
    assert parse_percent("0.30") == Decimal("30")
    assert parse_percent("32.5%") == Decimal("32.5")
    assert parse_percent("n/a") is None


def test_misc_parsers() -> None:
    assert parse_count("2,366") == 2366
    assert parse_count("12.5") is None
    assert parse_bool("Yes.") == "YES"
    assert parse_bool("NO") == "NO"
    assert parse_bool("maybe") is None
    assert parse_period("during 2026-03, yes") == "2026-03"
    assert parse_set("A; b ;  C c") == {"a", "b", "c c"}


def test_extract_flags_strict_format() -> None:
    text = (
        "Findings:\n"
        "FLAG: duplicate_line label:19000001\n"
        "flag: unknown_isrc staged:42\n"  # case-insensitive keyword
        "FLAG: nonsense without id\n"  # ignored — no source:id
    )
    assert extract_flags(text) == {
        ("duplicate_line", "label", 19000001),
        ("unknown_isrc", "staged", 42),
    }


# ── scalar scoring ───────────────────────────────────────────────────────────


def test_money_within_tolerance_passes() -> None:
    q = _q("money", "1234.567890", "0.01")
    assert score_t1(q, _out("ANSWER: $1,234.57")).passed
    assert score_t1(q, _out("ANSWER: 1234.567890 USD")).passed
    at_limit = score_t1(q, _out("ANSWER: $1234.577890"))
    assert at_limit.passed  # exactly 0.01 away
    beyond = score_t1(q, _out("ANSWER: $1234.578"))
    assert not beyond.passed
    assert beyond.detail.get("expected") == "1234.567890"


def test_money_missing_answer_line_fails_with_reason() -> None:
    q = _q("money", "10.00", "0.01")
    result = score_t1(q, _out("It comes to ten dollars."))
    assert not result.passed
    assert result.detail["failure"] == "no_answer_line"


def test_count_exact() -> None:
    q = _q("count", 9)
    assert score_t1(q, _out("ANSWER: 9")).passed
    assert not score_t1(q, _out("ANSWER: 10")).passed


def test_percent_exact_across_forms() -> None:
    q = _q("percent", "22")
    assert score_t1(q, _out("ANSWER: 22%")).passed
    assert score_t1(q, _out("ANSWER: 0.22")).passed
    assert not score_t1(q, _out("ANSWER: 24%")).passed


def test_set_order_free_and_diagnostic() -> None:
    q = _q("set", ["Alpha", "Beta Co", "Gamma"])
    assert score_t1(q, _out("ANSWER: gamma; Alpha;  Beta  Co")).passed
    partial = score_t1(q, _out("ANSWER: Alpha; Beta Co"))
    assert not partial.passed
    assert partial.detail["missing"] == ["gamma"]
    assert partial.score == 0.0


def test_unexpected_abstention_fails_t1() -> None:
    q = _q("money", "10.00", "0.01")
    result = score_t1(q, _out("ABSTAIN: cannot find it", abstained=True))
    assert not result.passed
    assert result.detail["failure"] == "abstained_unexpectedly"


def test_abstain_kind_scores_typed_flag_only() -> None:
    q = _q("abstain", "ABSTAIN")
    assert score_t1(q, _out("ABSTAIN: no such artist", abstained=True)).passed
    # Prose that "declines" without the typed protocol does not count.
    assert not score_t1(q, _out("I don't think that artist exists.")).passed


def test_incomplete_run_fails() -> None:
    q = _q("money", "10.00", "0.01")
    result = score_t1(q, _out("...", status="exhausted"))
    assert not result.passed
    assert result.detail["failure"] == "run_exhausted"


# ── reconciliation flags ─────────────────────────────────────────────────────


def _flags_q(expected_flags: list[tuple[str, int]], borderline: list[int]) -> Question:
    return Question(
        id="test-flags",
        category="reconciliation",
        agent="reconciler",
        tiers=["t1"],
        prompt="scan",
        answer_kind="flags",
        expected={
            "flags": [
                {"kind": kind, "source": "label", "line_id": line_id}
                for kind, line_id in expected_flags
            ],
            "borderline_line_ids": borderline,
        },
    )


def test_flags_perfect_scores_one() -> None:
    q = _flags_q([("duplicate_line", 11), ("unknown_isrc", 22)], borderline=[33])
    text = "FLAG: duplicate_line label:11\nFLAG: unknown_isrc label:22\nWithin tolerance: 33."
    result = score_t1(q, _out(text))
    assert result.passed and result.score == 1.0
    assert result.detail["precision"] == 1.0 and result.detail["recall"] == 1.0


def test_flags_precision_recall_math() -> None:
    q = _flags_q([("duplicate_line", 11), ("unknown_isrc", 22)], borderline=[])
    # One hit, one miss, one spurious extra: P=0.5, R=0.5, F1=0.5.
    result = score_t1(q, _out("FLAG: duplicate_line label:11\nFLAG: negative_units label:99"))
    assert not result.passed
    assert result.detail["precision"] == 0.5
    assert result.detail["recall"] == 0.5
    assert result.score == 0.5


def test_flagging_borderline_is_a_precision_failure() -> None:
    """§3.4's trap: the two seeded borderline cases are inside tolerance — flagging
    one is scored as a named failure, not just a lower F1."""
    q = _flags_q([("duplicate_line", 11)], borderline=[77])
    result = score_t1(
        q, _out("FLAG: duplicate_line label:11\nFLAG: sudden_territory_spike label:77")
    )
    assert not result.passed
    assert result.detail["borderline_flagged"] == [77]


def test_empty_expected_and_empty_answer_passes() -> None:
    q = _flags_q([], borderline=[])
    result = score_t1(q, _out("Nothing out of tolerance this period."))
    assert result.passed and result.score == 1.0


def test_empty_expected_with_spurious_flag_fails() -> None:
    q = _flags_q([], borderline=[])
    result = score_t1(q, _out("FLAG: duplicate_line label:5"))
    assert not result.passed and result.score == 0.0
