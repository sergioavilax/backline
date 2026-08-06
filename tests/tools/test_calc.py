"""calc_royalties: the DB-assembled ledger must reproduce the answer key (D-001).

The truth engine computed ``truth.expected_ledger`` from the *clean* in-memory world
through ``royaltycalc``. The runtime tool assembles the same inputs from Postgres —
dirty statement lines and all — so for every artist untouched by line-level anomalies
the tool must land on the answer key **exactly**, to the microdollar, for the full
12-period chain. (Tests may read ``truth``; agents may not — invariant 3.)
"""

from decimal import Decimal

import asyncpg
import pytest

from backline.config import get_settings
from backline.tools.calc import build_calc_royalties_tool
from backline.tools.context import ToolContext
from backline.tools.ledger import compute_ledger_slice, compute_spot_quote
from tests.conftest import requires_postgres

pytestmark = requires_postgres

LAST_PERIOD = "2026-06"


async def line_affected_artists(pool: asyncpg.Pool) -> dict[int, list[asyncpg.Record]]:
    """Artists whose *reported statement lines* diverge from the clean set, per registry.

    dashboard_gap corrupts the dashboard side (statement money stays authoritative) and
    borderline territory spikes are legit revenue (D-005) — neither affects the calc.
    unknown_isrc lines match no catalog entry, so they never attribute to an artist.
    """
    rows = await pool.fetch(
        """
        SELECT r.kind, r.statement_line_id, sl.period, sl.isrc, sl.upc,
               COALESCE(t.primary_artist_id, ra.artist_id) AS artist_id
        FROM truth.anomaly_registry r
        JOIN label.statement_lines sl ON sl.id = r.statement_line_id
        LEFT JOIN label.tracks t ON t.isrc = sl.isrc
        LEFT JOIN LATERAL (
            SELECT MIN(t2.primary_artist_id) AS artist_id
            FROM label.releases rel
            JOIN label.release_tracks rt ON rt.release_id = rel.id
            JOIN label.tracks t2 ON t2.id = rt.track_id
            WHERE rel.upc = sl.upc
            HAVING COUNT(DISTINCT t2.primary_artist_id) = 1
        ) ra ON sl.isrc = ''
        WHERE r.kind IN ('duplicate_line', 'currency_mismatch', 'negative_units',
                         'period_bleed')
           OR (r.kind = 'sudden_territory_spike' AND r.expected_flag_kind IS NOT NULL)
        """
    )
    affected: dict[int, list[asyncpg.Record]] = {}
    for row in rows:
        if row["artist_id"] is not None:
            affected.setdefault(row["artist_id"], []).append(row)
    return affected


async def truth_row(pool: asyncpg.Pool, artist_id: int, period: str) -> asyncpg.Record:
    row = await pool.fetchrow(
        "SELECT gross, recouped, net_payable, balance_after FROM truth.expected_ledger "
        "WHERE artist_id = $1 AND period = $2",
        artist_id,
        period,
    )
    assert row is not None
    return row


async def test_every_unaffected_artist_matches_truth_exactly(pool: asyncpg.Pool) -> None:
    affected = await line_affected_artists(pool)
    artist_ids = [r["id"] for r in await pool.fetch("SELECT id FROM label.artists ORDER BY id")]
    clean = [a for a in artist_ids if a not in affected]
    assert len(clean) >= 100  # ~40 anomalies can't touch a third of 150 artists

    mismatches: list[str] = []
    for artist_id in clean:
        slice_ = await compute_ledger_slice(pool, artist_id=artist_id, period=LAST_PERIOD)
        expected = await truth_row(pool, artist_id, LAST_PERIOD)
        got = (slice_.gross, slice_.recouped, slice_.net_payable, slice_.balance_after)
        want = (
            expected["gross"],
            expected["recouped"],
            expected["net_payable"],
            expected["balance_after"],
        )
        if got != want:
            mismatches.append(f"artist {artist_id}: got {got}, want {want}")
    assert not mismatches, (
        f"{len(mismatches)}/{len(clean)} clean artists diverge from the answer key "
        f"(the balance chain covers all 12 periods):\n" + "\n".join(mismatches[:5])
    )


async def test_special_artists_are_covered(pool: asyncpg.Pool) -> None:
    """MG, carve-out, and cross-collateral artists go through the same DB assembly."""
    affected = await line_affected_artists(pool)
    mg_artist = await pool.fetchval(
        """
        SELECT c.artist_id FROM label.contract_terms ct
        JOIN label.contracts c ON c.id = ct.contract_id
        WHERE ct.terms -> 'sections' -> 'advances_recoupment'
              ->> 'minimum_guarantee_per_period' IS NOT NULL
        LIMIT 1
        """
    )
    carveout_artist = await pool.fetchval(
        """
        SELECT c.artist_id FROM label.contract_terms ct
        JOIN label.contracts c ON c.id = ct.contract_id
        WHERE ct.terms -> 'sections' -> 'term_territory' -> 'excluded_territories'
              @> '["JP"]'::jsonb AND c.kind = 'base'
        LIMIT 1
        """
    )
    xcollat_artist = await pool.fetchval(
        "SELECT artist_id FROM label.recoup_accounts WHERE xcollat_group_id LIKE 'XC-%' "
        "ORDER BY artist_id LIMIT 1"
    )
    for artist_id in (mg_artist, carveout_artist, xcollat_artist):
        assert artist_id is not None
        if artist_id in affected:
            continue  # covered by the exclusion test below
        slice_ = await compute_ledger_slice(pool, artist_id=artist_id, period=LAST_PERIOD)
        expected = await truth_row(pool, artist_id, LAST_PERIOD)
        assert slice_.net_payable == expected["net_payable"]
        assert slice_.balance_after == expected["balance_after"]


async def test_dirty_lines_make_affected_artists_diverge(pool: asyncpg.Pool) -> None:
    """Sensitivity canary: the tool computes from *reported* lines, so a corrupted
    artist must NOT match truth — if it did, the equality test above proves nothing."""
    affected = await line_affected_artists(pool)
    case = next(
        (
            (artist_id, rows[0])
            for artist_id, rows in sorted(affected.items())
            if rows[0]["kind"] == "currency_mismatch"
        ),
        None,
    )
    assert case is not None
    artist_id, anomaly = case
    slice_ = await compute_ledger_slice(pool, artist_id=artist_id, period=anomaly["period"])
    expected = await truth_row(pool, artist_id, anomaly["period"])
    assert slice_.gross != expected["gross"]


async def test_excluding_flagged_lines_reconciles_to_truth(pool: asyncpg.Pool) -> None:
    """The Reconciler's contract: flag the injected lines, exclude them, match the key.

    (currency_mismatch corrupts a *real* line's currency field — the fix is a
    correction, not an exclusion — so those artists are skipped here.)
    """
    affected = await line_affected_artists(pool)
    tested = 0
    for artist_id, rows in sorted(affected.items()):
        kinds = {r["kind"] for r in rows}
        if "currency_mismatch" in kinds:
            continue
        exclude = [r["statement_line_id"] for r in rows]
        slice_ = await compute_ledger_slice(
            pool, artist_id=artist_id, period=LAST_PERIOD, exclude_line_ids=exclude
        )
        expected = await truth_row(pool, artist_id, LAST_PERIOD)
        assert slice_.gross == expected["gross"], f"artist {artist_id}"
        assert slice_.net_payable == expected["net_payable"], f"artist {artist_id}"
        assert slice_.balance_after == expected["balance_after"], f"artist {artist_id}"
        tested += 1
    assert tested >= 3


async def test_negative_units_are_auto_excluded_and_reported(pool: asyncpg.Pool) -> None:
    row = await pool.fetchrow(
        """
        SELECT sl.id, t.primary_artist_id AS artist_id, sl.period
        FROM truth.anomaly_registry r
        JOIN label.statement_lines sl ON sl.id = r.statement_line_id
        JOIN label.tracks t ON t.isrc = sl.isrc
        WHERE r.kind = 'negative_units'
        LIMIT 1
        """
    )
    assert row is not None
    slice_ = await compute_ledger_slice(pool, artist_id=row["artist_id"], period=row["period"])
    assert row["id"] in slice_.auto_excluded_line_ids


async def test_spot_quote_resolves_rates_from_terms(pool: asyncpg.Pool) -> None:
    """Spot rates must match the canonical terms JSON (an independent source)."""
    case = await pool.fetchrow(
        """
        SELECT c.artist_id, c.id AS contract_id,
               ct.terms -> 'sections' -> 'royalties' -> 'rate_card' AS rate_card
        FROM label.contracts c
        JOIN label.contract_terms ct ON ct.contract_id = c.id
        WHERE c.kind = 'base' AND c.effective_to IS NULL
          AND ct.terms -> 'sections' -> 'royalties' -> 'escalators' = '[]'::jsonb
          AND NOT EXISTS (SELECT 1 FROM label.amendments a
                          WHERE a.supersedes_contract_id = c.id)
          AND (SELECT count(*) FROM label.contracts c2
               WHERE c2.artist_id = c.artist_id AND c2.kind = 'base') = 1
        ORDER BY c.id LIMIT 1
        """
    )
    assert case is not None
    import json

    card = {
        (e["revenue_type"], e["territory"]): Decimal(e["rate"])
        for e in json.loads(case["rate_card"])
    }
    quote = await compute_spot_quote(
        pool,
        artist_id=case["artist_id"],
        rows=[("streaming", "US", Decimal("1000"), "USD")],
        as_of=None,
    )
    expected_rate = card.get(("streaming", "US"), card[("streaming", "WW")])
    assert quote.lines[0].rate == expected_rate
    assert quote.lines[0].royalty == Decimal("1000") * expected_rate
    assert quote.contract_id == case["contract_id"]


async def test_spot_quote_carveout_territory_earns_zero(pool: asyncpg.Pool) -> None:
    artist_id = await pool.fetchval(
        """
        SELECT c.artist_id FROM label.contract_terms ct
        JOIN label.contracts c ON c.id = ct.contract_id
        WHERE ct.terms -> 'sections' -> 'term_territory' -> 'excluded_territories'
              @> '["JP"]'::jsonb AND c.kind = 'base'
        LIMIT 1
        """
    )
    quote = await compute_spot_quote(
        pool,
        artist_id=artist_id,
        rows=[("streaming", "JP", Decimal("500"), "USD")],
        as_of=None,
    )
    assert quote.lines[0].rate == Decimal("0")
    assert quote.lines[0].royalty == Decimal("0")


async def test_tool_renders_ledger_and_validates(pool: asyncpg.Pool) -> None:
    ctx = ToolContext(pool=pool, settings=get_settings())
    tool = build_calc_royalties_tool(ctx)
    artist = await pool.fetchrow("SELECT id, stage_name FROM label.artists ORDER BY id LIMIT 1")
    out = await tool.handler(tool.params(artist=artist["stage_name"], period=LAST_PERIOD))
    expected = await truth_row(pool, artist["id"], LAST_PERIOD)
    assert str(expected["net_payable"].quantize(Decimal("0.01"))) in out
    assert "net payable" in out.lower()
    assert LAST_PERIOD in out

    with pytest.raises(ValueError, match="artist"):
        tool.params(period=LAST_PERIOD)  # neither artist nor artist_id
    with pytest.raises(ValueError, match=r"period|rows"):
        tool.params(artist="X")  # neither ledger nor spot mode


async def test_tool_unknown_artist_raises_with_candidates(pool: asyncpg.Pool) -> None:
    ctx = ToolContext(pool=pool, settings=get_settings())
    tool = build_calc_royalties_tool(ctx)
    with pytest.raises(LookupError, match=r"[Nn]o artist"):
        await tool.handler(tool.params(artist="Definitely Nobody", period=LAST_PERIOD))
