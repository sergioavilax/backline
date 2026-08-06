"""Registry-driven anomalies (§3.4): the registry plan drives the corruption, never the
other way around.

The clean world is the payable truth. Anomalies are corruptions of the *reporting*:
injected lines (duplicates, unknown ISRCs, negative adjustments, period bleed, territory
spikes), field corruption (currency_mismatch), or dashboard-side corruption
(dashboard_gap — the statement stays the money of record). The truth engine consumes the
clean set only; the DB and CSVs carry the dirty set. Two registered cases are borderline
(inside tolerance) with ``expected_flag_kind = NULL``: the correct behavior is NOT
flagging them.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import ROUND_HALF_EVEN, Decimal

import numpy as np

from backline.royaltycalc import money6
from datagen.config import period_add
from datagen.revenue import Cells, line_gross, line_hash, native_price
from datagen.world import Structure
from datagen.worldmodel import AnomalyEntry, DashboardStream, StatementLine


@dataclass
class AnomalyOutcome:
    dirty_lines: list[StatementLine]  # what statements/CSVs/DB carry
    clean_additions: list[StatementLine]  # borderline lines that are genuinely legit
    registry: list[AnomalyEntry]
    dashboard: list[DashboardStream]  # dirty dashboard (gap targets corrupted)


def _rehash(line: StatementLine, feed_key: str) -> StatementLine:
    return replace(
        line,
        line_hash=line_hash(
            feed_key,
            line.period,
            line.isrc,
            line.upc,
            line.store,
            line.territory,
            line.units,
            line.gross_amount,
            line.currency,
        ),
    )


def apply_anomalies(
    structure: Structure,
    cells: Cells,
    clean_by_period: dict[int, list[StatementLine]],
    gen: np.random.Generator,
) -> AnomalyOutcome:
    config = structure.config
    world = structure.world
    n_periods = config.n_periods
    store_by_name = {s.name: s for s in config.stores}
    feed_of_store = {s.name: s.feed for s in config.stores}
    dist_feed = {d.id: d.feed_key for d in world.distributors}
    statement_period = {
        (pidx + 1) * 100 + d.id: period_add(config.start_period, pidx)
        for pidx in range(n_periods)
        for d in world.distributors
    }
    statement_of = {
        (feed_key, period_add(config.start_period, pidx)): (pidx + 1) * 100 + d_id
        for pidx in range(n_periods)
        for d_id, feed_key in dist_feed.items()
    }
    track_by_isrc = {t.isrc: t for t in world.tracks}

    injected_seq: dict[int, int] = {}

    def inject_id(pidx: int) -> int:
        injected_seq[pidx] = injected_seq.get(pidx, 0) + 1
        return (pidx + 1) * 10_000_000 + 9_000_000 + injected_seq[pidx]

    dirty: dict[int, list[StatementLine]] = {p: list(rows) for p, rows in clean_by_period.items()}
    clean_additions: list[StatementLine] = []
    registry: list[AnomalyEntry] = []
    registry_id = 0
    targeted: set[int] = set()

    def register(kind: str, line_id: int, expected: str | None, note: str) -> None:
        nonlocal registry_id
        registry_id += 1
        registry.append(
            AnomalyEntry(
                id=registry_id,
                kind=kind,
                statement_line_id=line_id,
                expected_flag_kind=expected,
                note=note,
            )
        )

    def digital_pool(pidx: int, min_units: int) -> list[StatementLine]:
        return [
            ln
            for ln in clean_by_period[pidx]
            if ln.isrc
            and ln.id not in targeted
            and ln.units >= min_units
            and store_by_name[ln.store].revenue_type in ("streaming", "download")
        ]

    def spread_periods(count: int, lo: int = 0) -> list[int]:
        return [int(gen.integers(lo, n_periods)) for _ in range(count)]

    # ── borderline_territory_spike: a legit line in a fresh territory (clean AND dirty) ─
    for _ in range(config.anomalies.borderline_territory_spike):
        tc = cells.track_cells[0]  # the reference streaming store
        counts: dict[int, int] = {}
        for pos in tc.track_pos:
            counts[int(pos)] = counts.get(int(pos), 0) + 1
        candidates = sorted(pos for pos, n in counts.items() if 3 <= n <= 6)
        track_pos = candidates[int(gen.integers(0, len(candidates)))]
        track = world.tracks[track_pos]
        active_terr = {
            int(t) for p, t in zip(tc.track_pos, tc.terr_idx, strict=True) if int(p) == track_pos
        }
        fresh = [i for i in range(len(config.territories)) if i not in active_terr]
        terr = config.territories[fresh[int(gen.integers(0, len(fresh)))]]
        pidx = int(gen.integers(6, n_periods))
        period = period_add(config.start_period, pidx)
        base = [
            float(b)
            for p, b in zip(tc.track_pos, tc.base_units, strict=True)
            if int(p) == track_pos
        ]
        units = round(1.6 * float(np.median(np.array(base))))
        feed = config.feeds[tc.store.feed]
        price = native_price(
            cells.price_usd[(tc.store.name, terr)], feed.currency, config.fx_rates[period]
        )
        gross = line_gross(price, units, feed.currency)
        origin_upc = next(r.upc for r in world.releases if r.id == track.origin_release_id)
        line = StatementLine(
            id=inject_id(pidx),
            statement_id=statement_of[(feed.key, period)],
            period=period,
            isrc=track.isrc,
            upc=origin_upc,
            store=tc.store.name,
            territory=terr,
            units=units,
            gross_amount=gross,
            currency=feed.currency,
            line_hash="",
        )
        line = _rehash(line, feed.key)
        dirty[pidx].append(line)
        clean_additions.append(line)
        register(
            "sudden_territory_spike",
            line.id,
            None,
            f"borderline: first {terr} activity for {track.isrc} on {tc.store.name} in "
            f"{period} at ~1.6x its median cell — inside spike tolerance; must NOT flag",
        )

    # ── duplicate_line: exact copy, same line_hash, new id ───────────────────
    for pidx in spread_periods(config.anomalies.counts["duplicate_line"]):
        pool = digital_pool(pidx, min_units=120)
        src = pool[int(gen.integers(0, len(pool)))]
        targeted.add(src.id)
        dup = replace(src, id=inject_id(pidx))
        dirty[pidx].append(dup)
        register(
            "duplicate_line",
            dup.id,
            "duplicate_line",
            f"exact duplicate of line {src.id} ({src.isrc} {src.store} {src.territory} "
            f"{src.period}); same line_hash",
        )

    # ── unknown_isrc: line referencing no catalog track ──────────────────────
    for n, pidx in enumerate(spread_periods(config.anomalies.counts["unknown_isrc"]), start=1):
        period = period_add(config.start_period, pidx)
        digital_stores = [s for s in config.stores if s.revenue_type in ("streaming", "download")]
        store = digital_stores[int(gen.integers(0, len(digital_stores)))]
        feed = config.feeds[store.feed]
        terr = config.territories[int(gen.integers(0, len(config.territories)))]
        currency = feed.currency
        if feed.gbp_territories and terr in feed.gbp_territories:
            currency = "GBP"
        fake_isrc = f"QZFBR{period[2:4]}9{n:04d}"
        assert fake_isrc not in track_by_isrc
        units = int(gen.integers(150, 5001))
        price = native_price(cells.price_usd[(store.name, terr)], currency, config.fx_rates[period])
        gross = line_gross(price, units, currency)
        line = StatementLine(
            id=inject_id(pidx),
            statement_id=statement_of[(feed.key, period)],
            period=period,
            isrc=fake_isrc,
            upc=None,
            store=store.name,
            territory=terr,
            units=units,
            gross_amount=gross,
            currency=currency,
            line_hash="",
        )
        line = _rehash(line, feed.key)
        dirty[pidx].append(line)
        register(
            "unknown_isrc",
            line.id,
            "unknown_isrc",
            f"ISRC {fake_isrc} appears in no catalog table ({store.name} {terr} {period})",
        )

    # ── currency_mismatch: meridian (EUR dialect) line claiming USD ──────────
    for pidx in spread_periods(config.anomalies.counts["currency_mismatch"]):
        pool = [
            ln for ln in digital_pool(pidx, min_units=200) if feed_of_store[ln.store] == "meridian"
        ]
        src = pool[int(gen.integers(0, len(pool)))]
        targeted.add(src.id)
        mutated = _rehash(replace(src, currency="USD"), "meridian")
        rows = dirty[pidx]
        rows[rows.index(src)] = mutated
        register(
            "currency_mismatch",
            mutated.id,
            "currency_mismatch",
            f"meridian reports EUR but line {mutated.id} claims USD "
            f"({mutated.isrc} {mutated.store} {mutated.period}); amount unchanged",
        )

    # ── negative_units: injected negative adjustment ─────────────────────────
    for pidx in spread_periods(config.anomalies.counts["negative_units"]):
        pool = digital_pool(pidx, min_units=300)
        src = pool[int(gen.integers(0, len(pool)))]
        targeted.add(src.id)
        ratio = Decimal(str(round(float(gen.uniform(0.25, 0.45)), 2)))
        units_adj = -max(round(src.units * float(ratio)), 1)
        gross_adj = money6(src.gross_amount * Decimal(units_adj) / Decimal(src.units))
        if src.currency == "JPY":
            gross_adj = money6(gross_adj.quantize(Decimal("1"), rounding=ROUND_HALF_EVEN))
        feed_key = feed_of_store[src.store]
        line = StatementLine(
            id=inject_id(pidx),
            statement_id=src.statement_id,
            period=src.period,
            isrc=src.isrc,
            upc=src.upc,
            store=src.store,
            territory=src.territory,
            units=units_adj,
            gross_amount=gross_adj,
            currency=src.currency,
            line_hash="",
        )
        line = _rehash(line, feed_key)
        dirty[pidx].append(line)
        register(
            "negative_units",
            line.id,
            "negative_units",
            f"negative adjustment ({units_adj} units) against {src.isrc} {src.store} "
            f"{src.territory} {src.period}",
        )

    # ── period_bleed: late-reported prior-month line inside this month's drop ─
    for pidx in spread_periods(config.anomalies.counts["period_bleed"], lo=1):
        pool = digital_pool(pidx - 1, min_units=200)
        src = pool[int(gen.integers(0, len(pool)))]
        targeted.add(src.id)
        feed_key = feed_of_store[src.store]
        period = period_add(config.start_period, pidx)
        units = max(round(src.units * float(gen.uniform(0.3, 0.5))), 1)
        gross = money6(src.gross_amount * Decimal(units) / Decimal(src.units))
        if src.currency == "JPY":
            gross = money6(gross.quantize(Decimal("1"), rounding=ROUND_HALF_EVEN))
        line = StatementLine(
            id=inject_id(pidx),
            statement_id=statement_of[(feed_key, period)],
            period=src.period,  # dated outside the statement's period — the tell
            isrc=src.isrc,
            upc=src.upc,
            store=src.store,
            territory=src.territory,
            units=units,
            gross_amount=gross,
            currency=src.currency,
            line_hash="",
        )
        line = _rehash(line, feed_key)
        dirty[pidx].append(line)
        register(
            "period_bleed",
            line.id,
            "period_bleed",
            f"line dated {src.period} arrived in the {period} statement of {feed_key} "
            f"({src.isrc} {src.store})",
        )

    # ── sudden_territory_spike: units in a territory with zero history ───────
    for _ in range(config.anomalies.counts["sudden_territory_spike"]):
        tc_pool = [tc for tc in cells.track_cells if tc.store.revenue_type == "streaming"]
        tc = tc_pool[int(gen.integers(0, len(tc_pool)))]
        spike_counts: dict[int, int] = {}
        for pos in tc.track_pos:
            spike_counts[int(pos)] = spike_counts.get(int(pos), 0) + 1
        n_terr = len(config.territories)
        candidates = sorted(pos for pos, n in spike_counts.items() if 3 <= n < n_terr)
        track_pos = candidates[int(gen.integers(0, len(candidates)))]
        track = world.tracks[track_pos]
        active_terr = {
            int(t) for p, t in zip(tc.track_pos, tc.terr_idx, strict=True) if int(p) == track_pos
        }
        fresh = [i for i in range(len(config.territories)) if i not in active_terr]
        terr = config.territories[fresh[int(gen.integers(0, len(fresh)))]]
        pidx = int(gen.integers(6, n_periods))
        period = period_add(config.start_period, pidx)
        base = [
            float(b)
            for p, b in zip(tc.track_pos, tc.base_units, strict=True)
            if int(p) == track_pos
        ]
        units = round(float(np.max(np.array(base))) * float(gen.uniform(8.0, 15.0)))
        feed = config.feeds[tc.store.feed]
        price = native_price(
            cells.price_usd[(tc.store.name, terr)], feed.currency, config.fx_rates[period]
        )
        gross = line_gross(price, units, feed.currency)
        origin_upc = next(r.upc for r in world.releases if r.id == track.origin_release_id)
        line = StatementLine(
            id=inject_id(pidx),
            statement_id=statement_of[(feed.key, period)],
            period=period,
            isrc=track.isrc,
            upc=origin_upc,
            store=tc.store.name,
            territory=terr,
            units=units,
            gross_amount=gross,
            currency=feed.currency,
            line_hash="",
        )
        line = _rehash(line, feed.key)
        dirty[pidx].append(line)
        register(
            "sudden_territory_spike",
            line.id,
            "sudden_territory_spike",
            f"{units} units in {terr} for {track.isrc} on {tc.store.name} in {period} — "
            f"territory has zero prior history (artificial-streaming smell)",
        )

    # ── dashboard: aggregate dirty streaming lines, then corrupt gap targets ──
    streaming_stores = {s.name for s in config.stores if s.revenue_type == "streaming"}
    agg: dict[tuple[str, str, str], int] = {}
    contributors: dict[tuple[str, str, str], list[StatementLine]] = {}
    for pidx in range(n_periods):
        for ln in dirty[pidx]:
            if ln.store not in streaming_stores or not ln.isrc:
                continue
            key = (statement_period[ln.statement_id], ln.isrc, ln.store)
            agg[key] = agg.get(key, 0) + ln.units
            contributors.setdefault(key, []).append(ln)

    gap_total = config.anomalies.counts["dashboard_gap"] + config.anomalies.borderline_dashboard_gap
    eligible_keys = sorted(k for k, units in agg.items() if units >= 800)
    chosen_pos = gen.choice(len(eligible_keys), size=gap_total, replace=False)
    gap_keys = [eligible_keys[int(i)] for i in chosen_pos]
    tolerance = config.anomalies.dashboard_gap_tolerance_pct
    for n, key in enumerate(gap_keys):
        borderline = n >= config.anomalies.counts["dashboard_gap"]
        pct = 3.4 if borderline else float(gen.uniform(8.0, 18.0))
        sign = 1.0 if float(gen.random()) < 0.5 else -1.0
        shifted = round(agg[key] * (1.0 + sign * pct / 100.0))
        anchor = max(contributors[key], key=lambda ln: (ln.units, ln.id))
        period_key, isrc, store_name = key
        agg[key] = shifted
        register(
            "dashboard_gap",
            anchor.id,
            None if borderline else "dashboard_gap",
            (
                f"borderline: dashboard shows {shifted} vs statement total for "
                f"{isrc}/{store_name}/{period_key} — {pct:.1f}% gap, inside {tolerance:.0f}% "
                f"tolerance; must NOT flag"
                if borderline
                else f"dashboard shows {shifted} vs statement total for {isrc}/{store_name}/"
                f"{period_key} — {sign * pct:+.1f}% gap beyond {tolerance:.0f}% tolerance"
            ),
        )

    dashboard = [
        DashboardStream(period=dash_period, isrc=dash_isrc, store=dash_store, streams=units)
        for (dash_period, dash_isrc, dash_store), units in sorted(agg.items())
    ]

    dirty_lines = [
        ln for pidx in range(n_periods) for ln in sorted(dirty[pidx], key=lambda x: x.id)
    ]
    return AnomalyOutcome(
        dirty_lines=dirty_lines,
        clean_additions=clean_additions,
        registry=registry,
        dashboard=dashboard,
    )
