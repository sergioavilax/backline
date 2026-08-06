"""Suite generator tests (Phase 5): determinism, §5.2 shape, and answer-key integrity.

The committed ``evals/suites/core.json`` is a golden artifact like the world
fingerprint: regeneration must reproduce it byte-for-byte, its counts must match the
plan, and — against a seeded Postgres — its expectations must agree with the database
the agents will actually be evaluated on.
"""

import json
import re
from decimal import Decimal

import asyncpg
import pytest

from backline.config import get_settings
from evals.generate_suite import HAND_FILE, generate
from evals.types import CATEGORY_TARGETS, Suite, dump_suite, load_answer_key, load_suite
from evals.worldfacts import WorldFacts, build_facts
from tests.conftest import requires_postgres

SEED = get_settings().world_seed


@pytest.fixture(scope="module")
def suite() -> Suite:
    return load_suite("core")


@pytest.fixture(scope="module")
def regenerated() -> Suite:
    return generate(SEED)


def test_committed_suite_reproduces_exactly(suite: Suite, regenerated: Suite) -> None:
    """The golden-suite discipline: generation is deterministic and the committed file
    is exactly what the generator produces (same protocol as the world fingerprint)."""
    from evals.types import SUITES_DIR

    assert regenerated.suite_hash == suite.suite_hash
    assert (SUITES_DIR / "core.json").read_text(encoding="utf-8") == dump_suite(regenerated)


def test_no_new_scientific_notation_in_committed_suites(suite: Suite) -> None:
    """Ratchet (D-030): exactly three committed expected strings remain in scientific
    notation — byte-frozen with the suite because ``suite_hash`` keys the live baseline
    (D-028/D-029; grading normalizes, so '2E+1' scores as 20). No new E-notation may
    enter anywhere in a question; this set may only shrink, and only in a deliberate
    suite-regeneration + re-baseline PR (which also flips ``_pct_str`` to
    ``royaltycalc.pct_points``)."""
    frozen = {
        ("contract_terms-004", "2E+1"),
        ("hand-contract_terms-01", "3E+1"),
        ("hand-contract_terms-03", "3E+1"),
    }
    pattern = re.compile(r"\dE[+-]\d")
    offenders = {
        (q.id, str(q.expected))
        for q in suite.questions
        if pattern.search(json.dumps({"p": q.prompt, "e": q.expected, "m": q.meta}, default=str))
    }
    assert offenders == frozen
    assert not pattern.search(HAND_FILE.read_text(encoding="utf-8"))


def test_category_counts_match_plan(suite: Suite) -> None:
    assert suite.counts() == {k: v for k, v in sorted(CATEGORY_TARGETS.items())}
    assert len(suite.questions) == 133


def test_hand_cases_all_present_and_resolved(suite: Suite) -> None:
    hand_ids = {c["id"] for c in json.loads(HAND_FILE.read_text(encoding="utf-8"))["cases"]}
    assert len(hand_ids) == 25
    by_id = suite.by_id()
    assert hand_ids <= set(by_id)
    for question_id in hand_ids:
        question = by_id[question_id]
        assert question.source == "hand"
        assert "{" not in question.prompt, f"{question_id}: unresolved placeholder"


def test_subsets_marked(suite: Suite) -> None:
    gate = suite.subset("gate")
    smoke = suite.subset("smoke")
    assert len(gate) == 43
    assert len(smoke) == 10
    # Every hand case gates; every category is represented in the gate subset.
    assert all(q.in_gate for q in suite.questions if q.source == "hand")
    assert {q.category for q in gate} == set(CATEGORY_TARGETS)
    # Smoke covers one scriptable question per category (adversarial from hand).
    assert {q.category for q in smoke} == set(CATEGORY_TARGETS)


def test_amendment_coverage(suite: Suite) -> None:
    """§5.2: 10 of the contract_terms questions involve amendments."""
    terms = [q for q in suite.questions if q.category == "contract_terms"]
    amended = [
        q
        for q in terms
        if q.meta.get("amended")
        or q.meta.get("gold_clause_no") == "§A1"
        or q.meta.get("requires_history")
        # The day-before-effective boundary case: the base governs, but answering
        # requires reasoning about the amendment's effective date.
        or "amendment_effective" in q.meta
    ]
    assert len(amended) == 10


def test_tiers_follow_plan(suite: Suite) -> None:
    tier_map = {
        "catalog_lookup": {"t1"},
        "recoupment_state": {"t1"},
        "abstention": {"t1"},
        "royalty_math": {"t1", "t2"},
        "cross_collateral": {"t1", "t2"},
        "sql_analytics": {"t1", "t2"},
        "reconciliation": {"t1", "t2"},
        "contract_terms": {"t1", "t2", "t3"},
        "multi_step": {"t1", "t2", "t3"},
        "adversarial": {"t2"},
    }
    for question in suite.questions:
        assert set(question.tiers) == tier_map[question.category], question.id


def test_t2_checks_present_where_tiered(suite: Suite) -> None:
    for question in suite.questions:
        if "t2" in question.tiers:
            assert question.t2_checks, f"{question.id} has t2 tier but no checks"
        else:
            assert not question.t2_checks, f"{question.id} carries checks without t2"


def test_money_questions_carry_tolerance(suite: Suite) -> None:
    for question in suite.questions:
        if question.answer_kind == "money":
            assert question.tolerance is not None, question.id
            Decimal(question.tolerance)  # parses
            Decimal(str(question.expected))  # expected parses as money


def test_reconciliation_covers_all_periods_and_borderlines(suite: Suite) -> None:
    period_questions = [
        q
        for q in suite.questions
        if q.category == "reconciliation" and "statement_id" not in q.meta
    ]
    assert len(period_questions) == 12
    all_borderline = [
        line_id for q in period_questions for line_id in q.expected["borderline_line_ids"]
    ]
    # Both seeded borderline cases appear as explicit non-flag expectations.
    assert len(all_borderline) == 2
    total_flags = sum(len(q.expected["flags"]) for q in period_questions)
    assert total_flags == 38  # 40 registered anomalies - 2 borderline


# ── Postgres-backed integrity: the suite must agree with the DB agents see ────


@pytest.mark.usefixtures("world_env")
@requires_postgres
async def test_reference_sql_reproduces_expected(pool: asyncpg.Pool, suite: Suite) -> None:
    """Every analyst question ships reference SQL; running it against the seeded DB
    must reproduce the committed expectation exactly (count/value/money kinds)."""
    checked = 0
    for question in suite.questions:
        sql = question.meta.get("reference_sql")
        if sql is None or question.answer_kind == "abstain":
            continue
        row = await pool.fetchrow(sql)
        assert row is not None, question.id
        value = next(iter(row.values()))
        if question.answer_kind in {"count"}:
            assert int(value) == int(question.expected), question.id
        elif question.answer_kind == "money":
            assert Decimal(str(value)) == Decimal(str(question.expected)), question.id
        else:
            assert str(value) == str(question.expected), question.id
        checked += 1
    assert checked >= 15


@pytest.mark.usefixtures("world_env")
@requires_postgres
async def test_ledger_expectations_match_truth_schema(pool: asyncpg.Pool, suite: Suite) -> None:
    """Money questions with (artist, period, field) meta must equal truth.expected_ledger."""
    field_map = {
        "net_payable": "net_payable",
        "gross": "gross",
        "recouped": "recouped",
        "balance_after": "balance_after",
    }
    checked = 0
    for question in suite.questions:
        meta = question.meta
        if question.answer_kind != "money" or "artist_id" not in meta or "period" not in meta:
            continue
        column = field_map.get(meta.get("field", ""))
        if column is None and question.category in {"royalty_math", "recoupment_state"}:
            column = "net_payable" if "payable" in question.prompt else None
        if column is None:
            continue
        value = await pool.fetchval(
            f"SELECT {column} FROM truth.expected_ledger WHERE artist_id = $1 AND period = $2",
            meta["artist_id"],
            meta["period"],
        )
        assert value is not None, question.id
        assert Decimal(str(question.expected)) == value, question.id
        checked += 1
    assert checked >= 20


@pytest.mark.usefixtures("world_env")
@requires_postgres
async def test_reconciliation_expectations_match_registry(pool: asyncpg.Pool, suite: Suite) -> None:
    """Per-period flag expectations == truth.anomaly_registry grouped by statement
    period, borderlines excluded-but-listed."""
    registry = await pool.fetch(
        """
        SELECT ar.expected_flag_kind AS kind, ar.statement_line_id AS line_id,
               s.period, ar.expected_flag_kind IS NULL AS borderline
        FROM truth.anomaly_registry ar
        JOIN label.statement_lines sl ON sl.id = ar.statement_line_id
        JOIN label.statements s ON s.id = sl.statement_id
        """
    )
    expected_by_period: dict[str, set[tuple[str, int]]] = {}
    borderline_by_period: dict[str, set[int]] = {}
    for row in registry:
        if row["borderline"]:
            borderline_by_period.setdefault(row["period"], set()).add(row["line_id"])
        else:
            expected_by_period.setdefault(row["period"], set()).add((row["kind"], row["line_id"]))
    for question in suite.questions:
        if question.category != "reconciliation" or "statement_id" in question.meta:
            continue
        period = question.meta["period"]
        suite_flags = {(f["kind"], f["line_id"]) for f in question.expected["flags"]}
        assert suite_flags == expected_by_period.get(period, set()), question.id
        assert set(question.expected["borderline_line_ids"]) == borderline_by_period.get(
            period, set()
        ), question.id


@pytest.mark.usefixtures("world_env")
@requires_postgres
async def test_paid_over_sets_match_truth(pool: asyncpg.Pool, suite: Suite) -> None:
    for question in suite.questions:
        if question.category != "multi_step" or question.answer_kind != "set":
            continue
        rows = await pool.fetch(
            """
            SELECT a.stage_name FROM truth.expected_ledger el
            JOIN label.artists a ON a.id = el.artist_id
            WHERE el.period = $1 AND el.net_payable > $2::numeric
            """,
            question.meta["period"],
            question.meta["threshold"],
        )
        assert sorted(r["stage_name"] for r in rows) == question.expected, question.id


@requires_postgres
async def test_answer_key_loads_into_truth_schema(pool: asyncpg.Pool, suite: Suite) -> None:
    n = await load_answer_key(pool, suite)
    assert n == 133
    # Scoped to this suite's ids — other test modules load their own mini-suites
    # into the same table (the upsert is shared-table by design).
    suite_ids = [q.id for q in suite.questions]
    count = await pool.fetchval(
        "SELECT count(*) FROM truth.qa_answer_key WHERE question_id = ANY($1::text[])",
        suite_ids,
    )
    assert count == 133
    row = await pool.fetchrow(
        "SELECT answer, tolerance, category FROM truth.qa_answer_key WHERE question_id = $1",
        "hand-royalty_math-01",
    )
    assert row is not None
    assert row["category"] == "royalty_math"
    assert row["tolerance"] == Decimal("0.01")
    payload = json.loads(row["answer"])
    assert payload["kind"] == "money"
    assert payload["suite_hash"] == suite.suite_hash
    # Idempotent upsert.
    assert await load_answer_key(pool, suite) == 133
    assert (
        await pool.fetchval(
            "SELECT count(*) FROM truth.qa_answer_key WHERE question_id = ANY($1::text[])",
            suite_ids,
        )
        == 133
    )


@pytest.fixture(scope="module")
def facts() -> WorldFacts:
    return build_facts(SEED)


def test_abstention_targets_do_not_exist(suite: Suite, facts: WorldFacts) -> None:
    """Fake artists/contracts named in abstention questions are provably absent."""
    roster = {a.stage_name.casefold() for a in facts.world.artists} | {
        a.legal_name.casefold() for a in facts.world.artists
    }
    contract_ids = {c.id for c in facts.world.contracts}
    for question in suite.questions:
        if question.category != "abstention":
            continue
        fake_artist = question.meta.get("fake_artist")
        if fake_artist is not None:
            assert fake_artist.casefold() not in roster, question.id
        fake_contract = question.meta.get("fake_contract_id")
        if fake_contract is not None:
            assert fake_contract not in contract_ids, question.id


def test_money_questions_avoid_tainted_artists(suite: Suite, facts: WorldFacts) -> None:
    for question in suite.questions:
        if question.answer_kind != "money":
            continue
        artist_id = question.meta.get("artist_id")
        if artist_id is not None:
            assert artist_id not in facts.tainted_artists, question.id
