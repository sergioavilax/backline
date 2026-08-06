"""Canonical row streams + Postgres bulk load (COPY).

``table_rows`` is the single definition of what lands in each table and in what order —
the loader COPYs these records and the fingerprint hashes them, so "what we loaded" and
"what we fingerprint" cannot drift apart. JSONB values are pre-serialized canonical JSON
strings (sorted keys); asyncpg encodes ``str`` to jsonb natively, and one encoder in
``jsonutil`` handles Decimal-bearing structures everywhere.
"""

from __future__ import annotations

from typing import Any

import asyncpg

from backline.jsonutil import canonical_dumps
from backline.royaltycalc import money6
from datagen.worldmodel import World

Record = tuple[Any, ...]


def table_rows(world: World) -> dict[str, tuple[list[str], list[Record]]]:
    """{schema.table: (columns, records)} in FK-safe load order, rows sorted by PK."""
    amendments = sorted((c for c in world.contracts if c.kind == "amendment"), key=lambda c: c.id)
    return {
        "label.artists": (
            ["id", "stage_name", "legal_name", "joined_at", "country"],
            [
                (a.id, a.stage_name, a.legal_name, a.joined_at, a.country)
                for a in sorted(world.artists, key=lambda a: a.id)
            ],
        ),
        "label.releases": (
            ["id", "upc", "title", "imprint", "release_date"],
            [
                (r.id, r.upc, r.title, r.imprint, r.release_date)
                for r in sorted(world.releases, key=lambda r: r.id)
            ],
        ),
        "label.tracks": (
            ["id", "isrc", "title", "primary_artist_id", "duration_s"],
            [
                (t.id, t.isrc, t.title, t.primary_artist_id, t.duration_s)
                for t in sorted(world.tracks, key=lambda t: t.id)
            ],
        ),
        "label.release_tracks": (
            ["release_id", "track_id", "position"],
            [
                (rt.release_id, rt.track_id, rt.position)
                for rt in sorted(world.release_tracks, key=lambda rt: (rt.release_id, rt.track_id))
            ],
        ),
        "label.contracts": (
            ["id", "artist_id", "doc_path", "effective_from", "effective_to", "kind"],
            [
                (c.id, c.artist_id, c.doc_path, c.effective_from, c.effective_to, c.kind)
                for c in sorted(world.contracts, key=lambda c: c.id)
            ],
        ),
        "label.contract_terms": (
            ["contract_id", "terms"],
            [
                (c.id, canonical_dumps(c.terms_json))
                for c in sorted(world.contracts, key=lambda c: c.id)
            ],
        ),
        "label.amendments": (
            ["amendment_id", "supersedes_contract_id", "replaced_sections"],
            [(c.id, c.supersedes_contract_id, list(c.replaced_sections)) for c in amendments],
        ),
        "label.advances": (
            ["id", "artist_id", "contract_id", "amount", "currency", "granted_at"],
            [
                (a.id, a.artist_id, a.contract_id, a.amount, a.currency, a.granted_at)
                for a in sorted(world.advances, key=lambda a: a.id)
            ],
        ),
        "label.expenses": (
            ["id", "artist_id", "class", "amount", "currency", "incurred_at", "recoupable"],
            [
                (
                    e.id,
                    e.artist_id,
                    e.expense_class,
                    e.amount,
                    e.currency,
                    e.incurred_at,
                    e.recoupable,
                )
                for e in sorted(world.expenses, key=lambda e: e.id)
            ],
        ),
        "label.recoup_accounts": (
            ["artist_id", "xcollat_group_id", "opening_balance"],
            [
                (r.artist_id, r.xcollat_group_id, r.opening_balance)
                for r in sorted(
                    world.recoup_accounts, key=lambda r: (r.artist_id, r.xcollat_group_id)
                )
            ],
        ),
        "label.distributors": (
            ["id", "name", "dialect"],
            [(d.id, d.name, d.dialect) for d in sorted(world.distributors, key=lambda d: d.id)],
        ),
        "label.statements": (
            ["id", "distributor_id", "period", "received_at", "raw_path", "status"],
            [
                (s.id, s.distributor_id, s.period, s.received_at, s.raw_path, s.status)
                for s in sorted(world.statements, key=lambda s: s.id)
            ],
        ),
        "label.statement_lines": (
            [
                "id",
                "statement_id",
                "period",
                "isrc",
                "upc",
                "store",
                "territory",
                "units",
                "gross_amount",
                "currency",
                "line_hash",
            ],
            [
                (
                    line.id,
                    line.statement_id,
                    line.period,
                    line.isrc,
                    line.upc,
                    line.store,
                    line.territory,
                    line.units,
                    line.gross_amount,
                    line.currency,
                    line.line_hash,
                )
                for line in sorted(world.statement_lines, key=lambda line: line.id)
            ],
        ),
        "label.fx_rates": (
            ["period", "currency", "usd_rate"],
            [],  # filled by caller with config-aware rows (see fx_rows)
        ),
        "label.dashboard_streams": (
            ["period", "isrc", "store", "streams"],
            [
                (d.period, d.isrc, d.store, d.streams)
                for d in sorted(world.dashboard_streams, key=lambda d: (d.period, d.isrc, d.store))
            ],
        ),
        "truth.expected_ledger": (
            ["artist_id", "period", "gross", "recouped", "net_payable", "balance_after"],
            [
                (
                    row.artist_id,
                    row.period,
                    row.gross,
                    row.recouped,
                    # cent-rounded VALUE, re-scaled to the column's 6dp representation
                    money6(row.net_payable),
                    row.balance_after,
                )
                for row in sorted(world.ledger, key=lambda row: (row.artist_id, row.period))
            ],
        ),
        "truth.anomaly_registry": (
            ["id", "kind", "statement_line_id", "expected_flag_kind", "note"],
            [
                (a.id, a.kind, a.statement_line_id, a.expected_flag_kind, a.note)
                for a in sorted(world.anomalies, key=lambda a: a.id)
            ],
        ),
    }


def fx_rows(fx_rates: dict[str, dict[str, Any]], periods: tuple[str, ...]) -> list[Record]:
    return [
        (period, currency, fx_rates[period][currency])
        for period in sorted(periods)
        for currency in sorted(fx_rates[period])
    ]


TRUNCATE_SQL = """
TRUNCATE
    label.artists, label.releases, label.tracks, label.release_tracks,
    label.contracts, label.contract_terms, label.amendments, label.advances,
    label.expenses, label.recoup_accounts, label.distributors, label.statements,
    label.statement_lines, label.fx_rates, label.dashboard_streams,
    staging.statement_batches, staging.proposed_allocations, staging.flags,
    truth.expected_ledger, truth.anomaly_registry, truth.qa_answer_key
    RESTART IDENTITY CASCADE
"""


async def load_world(world: World, fx: list[Record], database_url: str) -> dict[str, int]:
    """Truncate the world schemas and bulk-load everything via binary COPY."""
    conn = await asyncpg.connect(database_url)
    counts: dict[str, int] = {}
    try:
        async with conn.transaction():
            await conn.execute(TRUNCATE_SQL)
            for qualified, (columns, records) in table_rows(world).items():
                if qualified == "label.fx_rates":
                    records = fx
                schema, table = qualified.split(".")
                await conn.copy_records_to_table(
                    table, schema_name=schema, columns=columns, records=records
                )
                counts[qualified] = len(records)
        await conn.execute("ANALYZE")
    finally:
        await conn.close()
    return counts


async def world_is_seeded(database_url: str) -> bool:
    conn = await asyncpg.connect(database_url)
    try:
        row = await conn.fetchval("SELECT EXISTS (SELECT 1 FROM label.artists)")
        return bool(row)
    finally:
        await conn.close()
