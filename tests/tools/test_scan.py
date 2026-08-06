"""scan_anomalies vs truth.anomaly_registry: exact precision & recall, whole world.

The registry is the exam key (§3.4): every registered non-borderline anomaly must be
found under its kind, the two borderline cases must NOT be flagged, and — the strict
part — the rules may flag *nothing else* across all 12 seeded periods. Skips without
DATABASE_URL.
"""

import asyncpg
import pytest

from backline.config import get_settings
from backline.tools.context import ToolContext
from backline.tools.scan import EXCLUDABLE_KINDS, build_scan_anomalies_tool, run_scan
from tests.conftest import WorldEnv, requires_postgres

pytestmark = requires_postgres

SEEDED_PERIODS = [f"2025-{m:02d}" for m in range(7, 13)] + [f"2026-{m:02d}" for m in range(1, 7)]


@pytest.fixture
def ctx(pool: asyncpg.Pool, world_env: WorldEnv) -> ToolContext:
    settings = get_settings().model_copy(update={"data_dir": str(world_env.data_dir)})
    return ToolContext(pool=pool, settings=settings)


async def _registry(pool: asyncpg.Pool) -> tuple[set[tuple[str, int]], set[int]]:
    """(expected {(kind, line_id)} for non-borderline, {line_id} for borderline)."""
    rows = await pool.fetch(
        """
        SELECT r.kind, r.statement_line_id, r.expected_flag_kind
        FROM truth.anomaly_registry r
        """
    )
    expected = {
        (r["expected_flag_kind"], r["statement_line_id"])
        for r in rows
        if r["expected_flag_kind"] is not None
    }
    borderline = {r["statement_line_id"] for r in rows if r["expected_flag_kind"] is None}
    assert len(expected) >= 35 and len(borderline) == 2  # the §3.4 plan shape
    return expected, borderline


async def test_scan_matches_the_registry_exactly_across_all_periods(
    pool: asyncpg.Pool, world_env: WorldEnv
) -> None:
    expected, borderline = await _registry(pool)

    got: set[tuple[str, int]] = set()
    tolerance_mentions = 0
    for period in SEEDED_PERIODS:
        report = await run_scan(pool, period=period)
        assert report.n_statements == 6, f"{period}: expected all six feeds"
        got.update((c.kind, c.line_id) for c in report.candidates if c.source == "label")
        tolerance_mentions += len(report.within_tolerance)

    missed = expected - got
    extras = got - expected
    assert not missed, f"registry anomalies the rules missed (recall failure): {missed}"
    assert not extras, f"flags with no registry backing (precision failure): {extras}"

    flagged_ids = {line_id for _, line_id in got}
    assert not (flagged_ids & borderline), "borderline cases must never be flagged"
    # Both borderline cases sit inside tolerance and are *measured*, not flagged.
    assert tolerance_mentions >= 2


async def test_single_statement_scope_narrows_the_scan(pool: asyncpg.Pool) -> None:
    expected, _ = await _registry(pool)
    # Find the statement carrying one known duplicate_line anomaly.
    kind, line_id = next((k, i) for k, i in sorted(expected) if k == "duplicate_line")
    statement_id, period = await pool.fetchrow(
        "SELECT l.statement_id, s.period FROM label.statement_lines l "
        "JOIN label.statements s ON s.id = l.statement_id WHERE l.id = $1",
        line_id,
    )

    scoped = await run_scan(pool, period=period, statement_id=statement_id)
    assert scoped.n_statements == 1
    assert any(c.kind == kind and c.line_id == line_id for c in scoped.candidates)
    assert all(c.statement_id == statement_id for c in scoped.candidates)


async def test_suggested_exclusions_cover_excludable_kinds_only(pool: asyncpg.Pool) -> None:
    report = await run_scan(pool, period="2026-02")
    label_excl, staged_excl = report.suggested_exclusions()
    assert staged_excl == []  # seeded periods are fully ingested
    excludable_ids = {c.line_id for c in report.candidates if c.kind in EXCLUDABLE_KINDS}
    gap_only_ids = {
        c.line_id for c in report.candidates if c.kind == "dashboard_gap"
    } - excludable_ids
    assert set(label_excl) == excludable_ids
    assert not (set(label_excl) & gap_only_ids)  # statement money stays authoritative


async def test_tool_renders_evidence_and_exclusions(ctx: ToolContext) -> None:
    tool = build_scan_anomalies_tool(ctx)
    # A period guaranteed to carry candidates: pick one with registry rows.
    row = await ctx.pool.fetchrow(
        """
        SELECT s.period, count(*) AS n
        FROM truth.anomaly_registry r
        JOIN label.statement_lines l ON l.id = r.statement_line_id
        JOIN label.statements s ON s.id = l.statement_id
        WHERE r.expected_flag_kind IS NOT NULL
        GROUP BY s.period ORDER BY n DESC, s.period LIMIT 1
        """
    )
    out = await tool.handler(tool.params(period=row["period"]))
    assert f"period {row['period']}" in out
    assert "candidate flag(s)" in out
    assert "exclude_line_ids=[" in out
    assert "statement" in out


async def test_unknown_period_reports_gracefully(ctx: ToolContext) -> None:
    tool = build_scan_anomalies_tool(ctx)
    out = await tool.handler(tool.params(period="2031-01"))
    assert "no statements found" in out
