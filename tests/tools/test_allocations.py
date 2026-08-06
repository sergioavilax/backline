"""compute_allocations: one engine, whole-period batch, exclusions honored.

With the registry's injected lines excluded, every computed allocation must equal
``truth.expected_ledger`` — D-001 proven through the batch path — except artists
carrying the in-place ``currency_mismatch`` corruption (their reported value *is*
wrong; exclusion removes real revenue, so they legitimately diverge from clean
truth either way). Skips without DATABASE_URL.
"""

import re
from decimal import Decimal

import asyncpg
import pytest

from backline.config import get_settings
from backline.tools.allocations import build_compute_allocations_tool
from backline.tools.context import ToolContext
from tests.conftest import WorldEnv, requires_postgres

pytestmark = requires_postgres

PERIOD = "2026-02"
_ROW = re.compile(r"artist (\d+) \(.*?\): net_payable ([\d.]+)")


@pytest.fixture
def ctx(pool: asyncpg.Pool, world_env: WorldEnv) -> ToolContext:
    settings = get_settings().model_copy(update={"data_dir": str(world_env.data_dir)})
    return ToolContext(pool=pool, settings=settings)


async def test_allocations_match_truth_with_registry_exclusions(
    ctx: ToolContext, pool: asyncpg.Pool
) -> None:
    injected = await pool.fetch(
        "SELECT statement_line_id FROM truth.anomaly_registry "
        "WHERE expected_flag_kind IN ('duplicate_line', 'unknown_isrc', 'negative_units', "
        "'period_bleed', 'sudden_territory_spike')"
    )
    currency_artists = {
        r["artist_id"]
        for r in await pool.fetch(
            """
            SELECT DISTINCT t.primary_artist_id AS artist_id
            FROM truth.anomaly_registry reg
            JOIN label.statement_lines l ON l.id = reg.statement_line_id
            JOIN label.tracks t ON t.isrc = l.isrc
            WHERE reg.kind = 'currency_mismatch'
            """
        )
    }

    tool = build_compute_allocations_tool(ctx)
    out = await tool.handler(
        tool.params(
            period=PERIOD,
            exclude_line_ids=[r["statement_line_id"] for r in injected],
            min_net_payable=Decimal("0"),
        )
    )

    computed = {int(a): Decimal(n) for a, n in _ROW.findall(out)}
    assert len(computed) >= 60  # a healthy share of the roster pays out this period

    truth = {
        r["artist_id"]: r["net_payable"]
        for r in await pool.fetch(
            "SELECT artist_id, net_payable FROM truth.expected_ledger WHERE period = $1",
            PERIOD,
        )
    }
    mismatched = {
        artist: (net, truth[artist])
        for artist, net in computed.items()
        if artist not in currency_artists and truth[artist] != net
    }
    assert not mismatched, f"allocations diverge from the answer key: {mismatched}"

    # Completeness: every clean artist truth says to pay appears in the listing.
    should_pay = {
        artist for artist, net in truth.items() if net > 0 and artist not in currency_artists
    }
    missing = should_pay - set(computed)
    assert not missing, f"artists truth pays but the batch omits: {missing}"

    assert "Excluded label lines honored" in out


async def test_materiality_floor_lists_less_but_reports_coverage(ctx: ToolContext) -> None:
    tool = build_compute_allocations_tool(ctx)
    out = await tool.handler(tool.params(period=PERIOD, min_net_payable=Decimal("500")))
    computed = _ROW.findall(out)
    assert all(Decimal(net) >= 500 for _, net in computed)
    assert "below the floor" in out
    assert "report this coverage" in out


async def test_unknown_period_says_so(ctx: ToolContext) -> None:
    tool = build_compute_allocations_tool(ctx)
    out = await tool.handler(tool.params(period="2031-05"))
    assert "No artists have reported lines" in out
