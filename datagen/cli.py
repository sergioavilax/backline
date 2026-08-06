"""datagen CLI: seed | emit-period | fingerprint | corpus-tokens.

- ``seed``          builds the whole world (DB + /data/contracts + /data/inbox), < 3 min
- ``emit-period``   drops a *new* month into /data/inbox like a real distributor feed
- ``fingerprint``   prints the world fingerprint (optionally from the live DB)
- ``corpus-tokens`` prints corpus token counts (the scale-claim evidence)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from decimal import Decimal
from pathlib import Path

from backline.config import get_settings
from datagen.assemble import BuiltWorld, build_world
from datagen.config import WorldConfig, load_world_config, period_add, period_index
from datagen.corpus import count_corpus, format_report
from datagen.dbload import fx_rows, load_world, world_is_seeded
from datagen.feeds import drop_filename, render_feed_csv
from datagen.fingerprint import (
    combined_hash,
    fingerprint_files,
    fingerprint_from_db,
    fingerprint_from_world,
)
from datagen.pdfrender import render_all_contracts
from datagen.revenue import build_cells, build_statements, synth_period
from datagen.rng import emit_anomaly_generator, world_generator
from datagen.world import build_structure
from datagen.worldmodel import Statement, StatementLine


def _write_inbox_files(
    built: BuiltWorld, lines: list[StatementLine], data_dir: Path, period_indices: list[int]
) -> int:
    """Render the dirty lines of the given periods into per-(feed, period) drops."""
    config = built.structure.config
    inbox = data_dir / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    dist_by_id = {d.id: d for d in built.structure.world.distributors}
    by_statement: dict[int, list[StatementLine]] = {}
    for line in lines:
        by_statement.setdefault(line.statement_id, []).append(line)
    written = 0
    for pidx in period_indices:
        period = period_add(config.start_period, pidx)
        for dist in dist_by_id.values():
            statement_id = (pidx + 1) * 100 + dist.id
            feed = config.feeds[dist.feed_key]
            content = render_feed_csv(feed.dialect, by_statement.get(statement_id, []), config)
            (inbox / drop_filename(feed.dialect, period)).write_text(content, encoding="utf-8")
            written += 1
    return written


def cmd_seed(args: argparse.Namespace) -> int:
    settings = get_settings()
    data_dir = Path(settings.data_dir)
    started = time.perf_counter()

    if args.if_empty and asyncio.run(world_is_seeded(settings.database_url)):
        print("seed: world already present (label.artists non-empty) — skipping (--if-empty)")
        return 0

    config = load_world_config()
    print(f"seed: building Foldback Records (seed={settings.world_seed}) ...")
    t0 = time.perf_counter()
    built = build_world(config, settings.world_seed)
    world = built.world
    print(
        f"  world built in {time.perf_counter() - t0:.1f}s — "
        f"{len(world.artists)} artists, {len(world.contracts)} contracts, "
        f"{len(world.releases)} releases, {len(world.tracks)} tracks, "
        f"{len(world.statement_lines):,} statement lines, "
        f"{len(world.anomalies)} registered anomalies"
    )

    t0 = time.perf_counter()
    n_pdfs = render_all_contracts(built.structure, data_dir)
    print(f"  {n_pdfs} contract PDFs (+txt) rendered in {time.perf_counter() - t0:.1f}s")

    t0 = time.perf_counter()
    n_files = _write_inbox_files(
        built, world.statement_lines, data_dir, list(range(config.n_periods))
    )
    print(f"  {n_files} inbox drops written in {time.perf_counter() - t0:.1f}s")

    t0 = time.perf_counter()
    fx = fx_rows(config.fx_rates, config.periods)
    counts = asyncio.run(load_world(world, fx, settings.database_url))
    total_rows = sum(counts.values())
    print(
        f"  {total_rows:,} rows loaded into Postgres in {time.perf_counter() - t0:.1f}s "
        f"({counts['label.statement_lines']:,} statement lines)"
    )

    tables = fingerprint_from_world(world, fx)
    files = fingerprint_files(data_dir)
    print(f"  fingerprint: {combined_hash(tables, files)}")
    print(f"seed: done in {time.perf_counter() - started:.1f}s")
    return 0


def cmd_emit_period(args: argparse.Namespace) -> int:
    settings = get_settings()
    data_dir = Path(settings.data_dir)
    config = load_world_config()
    period = args.period
    idx = period_index(config.start_period, period)
    if idx < config.n_periods:
        print(
            f"emit-period: {period} is inside the seeded window "
            f"({config.start_period}..{config.periods[-1]}) — nothing to emit",
            file=sys.stderr,
        )
        return 1
    if period not in config.fx_rates:
        print(
            f"emit-period: no FX row for {period} in world.yaml — add one first",
            file=sys.stderr,
        )
        return 1
    if not asyncio.run(world_is_seeded(settings.database_url)):
        print("emit-period: world not seeded yet — run `make seed` first", file=sys.stderr)
        return 1

    print(f"emit-period: synthesizing {period} (seed={settings.world_seed}) ...")
    built = build_world_structure_only(config, settings.world_seed)
    lines = synth_period(built.structure, built.cells, idx)
    lines = _inject_emitted_anomalies(built, lines, idx)
    statements = build_statements(built.structure, [idx], status="received")

    n_files = _write_inbox_files(built, lines, data_dir, [idx])
    inserted = asyncio.run(_insert_received_statements(settings.database_url, statements))
    fx_inserted = asyncio.run(
        _insert_fx_rows(settings.database_url, period, config.fx_rates[period])
    )
    print(
        f"emit-period: {period} — {len(lines):,} lines across {n_files} drops in "
        f"data/inbox; {inserted} statement rows recorded (status=received), "
        f"{fx_inserted} FX rows added. The Reconciler takes it from here."
    )
    return 0


def build_world_structure_only(config: WorldConfig, seed: int) -> BuiltWorld:
    """Structure + cells without the seeded window's lines/anomalies/truth."""
    gen = world_generator(seed)
    structure = build_structure(config, seed, gen)
    cells = build_cells(structure, gen)
    return BuiltWorld(structure=structure, cells=cells, world=structure.world)


def _inject_emitted_anomalies(
    built: BuiltWorld, lines: list[StatementLine], pidx: int
) -> list[StatementLine]:
    """A few unregistered anomalies so a fresh drop gives the Reconciler real flags."""
    from dataclasses import replace

    from backline.royaltycalc import money6

    config = built.structure.config
    gen = emit_anomaly_generator(built.structure.seed, pidx)
    out = list(lines)
    digital = [ln for ln in out if ln.isrc and ln.units >= 150]
    n = min(config.anomalies.emitted_period_anomalies, len(digital))
    seq = 0

    def inject_id() -> int:
        nonlocal seq
        seq += 1
        return (pidx + 1) * 10_000_000 + 9_000_000 + seq

    for kind_idx in range(n):
        src = digital[int(gen.integers(0, len(digital)))]
        if kind_idx % 3 == 0:  # duplicate
            out.append(replace(src, id=inject_id()))
        elif kind_idx % 3 == 1:  # unknown isrc
            fake = f"QZFBR{src.period[2:4]}9{9000 + kind_idx:04d}"
            mutated = replace(src, id=inject_id(), isrc=fake, upc=None)
            out.append(mutated)
        else:  # negative adjustment
            units = -max(round(src.units * 0.3), 1)
            gross = money6(src.gross_amount * Decimal(units) / Decimal(src.units))
            out.append(replace(src, id=inject_id(), units=units, gross_amount=gross))
    return out


async def _insert_fx_rows(database_url: str, period: str, rates: dict[str, Decimal]) -> int:
    """The emitted month's FX rows (from world.yaml), so the runtime calculator can
    FX-normalize staged lines for that period. Idempotent; seeded periods untouched."""
    import asyncpg

    conn = await asyncpg.connect(database_url)
    inserted = 0
    try:
        for currency in sorted(rates):
            result = await conn.execute(
                "INSERT INTO label.fx_rates (period, currency, usd_rate) "
                "VALUES ($1, $2, $3) ON CONFLICT (period, currency) DO NOTHING",
                period,
                currency,
                rates[currency],
            )
            if result.endswith("1"):
                inserted += 1
    finally:
        await conn.close()
    return inserted


async def _insert_received_statements(database_url: str, statements: list[Statement]) -> int:
    import asyncpg

    conn = await asyncpg.connect(database_url)
    inserted = 0
    try:
        for statement in statements:
            ingested = await conn.fetchval(
                "SELECT status FROM label.statements WHERE id = $1", statement.id
            )
            if ingested == "ingested":
                raise RuntimeError(
                    f"statement {statement.id} ({statement.period}) already ingested — "
                    f"emit-period refuses to overwrite it"
                )
            result = await conn.execute(
                """
                INSERT INTO label.statements
                    (id, distributor_id, period, received_at, raw_path, status)
                VALUES ($1, $2, $3, $4, $5, $6)
                ON CONFLICT (id) DO NOTHING
                """,
                statement.id,
                statement.distributor_id,
                statement.period,
                statement.received_at,
                statement.raw_path,
                statement.status,
            )
            if result.endswith("1"):
                inserted += 1
    finally:
        await conn.close()
    return inserted


def cmd_fingerprint(args: argparse.Namespace) -> int:
    settings = get_settings()
    config = load_world_config()
    fx = fx_rows(config.fx_rates, config.periods)
    if args.from_db:
        tables = asyncio.run(fingerprint_from_db(settings.database_url))
    else:
        built = build_world(config, settings.world_seed)
        tables = fingerprint_from_world(built.world, fx)
    files = fingerprint_files(Path(settings.data_dir)) if args.files else None
    payload = {
        "seed": settings.world_seed,
        "tables": tables,
        **({"files": files} if files is not None else {}),
        "combined": combined_hash(tables, files),
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


def cmd_corpus_tokens(_args: argparse.Namespace) -> int:
    settings = get_settings()
    print(format_report(count_corpus(Path(settings.data_dir))))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="datagen", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_seed = sub.add_parser("seed", help="build the full world into Postgres + /data")
    p_seed.add_argument(
        "--if-empty",
        action="store_true",
        help="skip if the world is already seeded (compose init uses this)",
    )
    p_seed.set_defaults(func=cmd_seed)

    p_emit = sub.add_parser("emit-period", help="emit a new month's drops into /data/inbox")
    p_emit.add_argument("period", help='month to emit, e.g. "2026-07"')
    p_emit.set_defaults(func=cmd_emit_period)

    p_fp = sub.add_parser("fingerprint", help="print the deterministic world fingerprint")
    p_fp.add_argument("--from-db", action="store_true", help="fingerprint the live DB instead")
    p_fp.add_argument("--files", action="store_true", help="include /data file hashes")
    p_fp.set_defaults(func=cmd_fingerprint)

    p_ct = sub.add_parser("corpus-tokens", help="print corpus token counts")
    p_ct.set_defaults(func=cmd_corpus_tokens)

    args = parser.parse_args(argv)
    result: int = args.func(args)
    return result
