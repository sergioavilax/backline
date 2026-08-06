"""Revenue synthesis: activity cells (world stream) + monthly volumes (period streams).

Activity structure — which track sells where — is drawn once from the world stream.
Per-month volume jitter comes from an independent per-period stream so that
``datagen emit-period`` can synthesize any future month without replaying the seeded
window. Long-tail shape: a few hit tracks are active in most storexterritory cells,
tail tracks in a handful; every line's money is Decimal from birth (units x Decimal
unit price, quantized once).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import timedelta
from decimal import ROUND_HALF_EVEN, Decimal

import numpy as np

from backline.royaltycalc import money6
from datagen.config import StoreCfg, period_add, period_end_date
from datagen.rng import period_generator
from datagen.world import Structure
from datagen.worldmodel import Statement, StatementLine

ONE_YEN = Decimal("1")
PRICE_QUANTUM = Decimal("0.00000001")


@dataclass
class TrackCells:
    store: StoreCfg
    track_pos: np.ndarray  # index into world.tracks
    terr_idx: np.ndarray  # index into config.territories
    base_units: np.ndarray  # float64 expected monthly units
    comp_release_pos: np.ndarray  # -1 or index into world.releases (compilation UPC to report)


@dataclass
class ReleaseCells:
    store: StoreCfg
    release_pos: np.ndarray  # index into world.releases (physical, non-compilation)
    terr_idx: np.ndarray
    base_units: np.ndarray


@dataclass
class Cells:
    track_cells: list[TrackCells]
    release_cells: list[ReleaseCells]
    price_usd: dict[tuple[str, str], Decimal]  # (store, territory) -> USD unit price
    track_origin_pidx: np.ndarray  # per track: origin period index relative to start_period


def build_cells(structure: Structure, gen: np.random.Generator) -> Cells:
    config = structure.config
    world = structure.world
    territories = list(config.territories)
    terr_w = np.array([config.territory_weights[t] for t in territories])
    terr_share = terr_w / terr_w.sum()

    pops = np.array([structure.track_pop[t.id] for t in world.tracks])
    share = pops / pops.mean()

    start_y, start_m = int(config.start_period[:4]), int(config.start_period[5:7])
    track_origin_pidx = np.array(
        [
            (t.origin_release_date.year * 12 + t.origin_release_date.month)
            - (start_y * 12 + start_m)
            for t in world.tracks
        ]
    )

    # Compilation membership: track -> ordered list of compilation release positions.
    comp_positions: dict[int, list[int]] = {}
    release_pos_by_id = {r.id: i for i, r in enumerate(world.releases)}
    track_pos_by_id = {t.id: i for i, t in enumerate(world.tracks)}
    for rt in world.release_tracks:
        release = world.releases[release_pos_by_id[rt.release_id]]
        if release.is_compilation:
            comp_positions.setdefault(track_pos_by_id[rt.track_id], []).append(
                release_pos_by_id[rt.release_id]
            )

    price_usd: dict[tuple[str, str], Decimal] = {}
    track_cells: list[TrackCells] = []
    release_cells: list[ReleaseCells] = []

    physical_releases = [
        (i, r) for i, r in enumerate(world.releases) if r.physical and not r.is_compilation
    ]

    for store in config.stores:
        for territory in territories:
            factor = Decimal(str(round(float(gen.uniform(0.78, 1.15)), 4)))
            price_usd[(store.name, territory)] = store.unit_rate_usd * factor

        bias = np.array([store.territory_bias.get(t, 1.0) for t in territories])
        if store.revenue_type in ("streaming", "download"):
            act = config.revenue.activation[store.revenue_type]
            p_active = np.clip(act * store.scale * np.outer(share, terr_share * bias), 0.0, 0.97)
            active = gen.random(p_active.shape) < p_active
            t_pos, r_pos = np.nonzero(active)
            base = (
                config.revenue.base_monthly_units[store.revenue_type]
                * store.scale
                * share[t_pos]
                * (terr_share * bias)[r_pos]
                * gen.lognormal(mean=0.0, sigma=0.55, size=len(t_pos))
            )
            comp_attr = np.full(len(t_pos), -1, dtype=np.int64)
            if comp_positions and store.revenue_type == "streaming":
                for i, tp in enumerate(t_pos):
                    comps = comp_positions.get(int(tp))
                    if comps and float(gen.random()) < config.revenue.compilation_upc_share:
                        comp_attr[i] = comps[int(gen.integers(0, len(comps)))]
            track_cells.append(
                TrackCells(
                    store=store,
                    track_pos=t_pos.astype(np.int64),
                    terr_idx=r_pos.astype(np.int64),
                    base_units=base,
                    comp_release_pos=comp_attr,
                )
            )
        elif store.revenue_type == "physical":
            if not physical_releases:
                continue
            rel_share = np.array(
                [structure.artist_pop[r.primary_artist_id or 1] for _, r in physical_releases]
            )
            rel_share = rel_share / rel_share.mean()
            act = config.revenue.activation["physical"]
            p_active = np.clip(
                act * store.scale * np.outer(rel_share, terr_share * bias), 0.0, 0.92
            )
            active = gen.random(p_active.shape) < p_active
            rl_pos, r_pos = np.nonzero(active)
            base = (
                config.revenue.base_monthly_units["physical"]
                * store.scale
                * rel_share[rl_pos]
                * (terr_share * bias)[r_pos]
                * gen.lognormal(mean=0.0, sigma=0.5, size=len(rl_pos))
            )
            release_cells.append(
                ReleaseCells(
                    store=store,
                    release_pos=np.array(
                        [physical_releases[int(i)][0] for i in rl_pos], dtype=np.int64
                    ),
                    terr_idx=r_pos.astype(np.int64),
                    base_units=base,
                )
            )
        # sync has no standing cells — lines are drawn per period

    return Cells(
        track_cells=track_cells,
        release_cells=release_cells,
        price_usd=price_usd,
        track_origin_pidx=track_origin_pidx,
    )


def line_hash(
    feed_key: str,
    period: str,
    isrc: str,
    upc: str | None,
    store: str,
    territory: str,
    units: int,
    gross: Decimal,
    currency: str,
) -> str:
    payload = "|".join(
        [feed_key, period, isrc, upc or "", store, territory, str(units), str(gross), currency]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def native_price(price_usd: Decimal, currency: str, fx: dict[str, Decimal]) -> Decimal:
    """USD unit price -> feed-currency unit price at the period's fixed rate, 8dp."""
    return (price_usd / fx[currency]).quantize(PRICE_QUANTUM, rounding=ROUND_HALF_EVEN)


def line_gross(price_native: Decimal, units: int, currency: str) -> Decimal:
    """Units x native unit price, quantized once. JPY feeds report whole yen (world
    realism: the feed's own rounding, applied at generation — royalty math consumes the
    reported value unchanged through money6)."""
    raw = price_native * units
    if currency == "JPY":
        return money6(raw.quantize(ONE_YEN, rounding=ROUND_HALF_EVEN))
    return money6(raw)


def _ramp(months_since: int, ramp: tuple[float, ...]) -> float:
    if months_since < 0:
        return 0.0
    if months_since < len(ramp):
        return ramp[months_since]
    return max(0.988 ** (months_since - len(ramp)), 0.55)


def synth_period(
    structure: Structure, cells: Cells, absolute_period_index: int
) -> list[StatementLine]:
    """Clean statement lines for one month, drawn from that month's period stream."""
    config = structure.config
    world = structure.world
    period = period_add(config.start_period, absolute_period_index)
    pgen = period_generator(structure.seed, absolute_period_index)
    territories = list(config.territories)
    fx = config.fx_rates[period]
    seasonal = config.revenue.seasonal[period[5:7]]
    dist_by_feed = {d.feed_key: d.id for d in world.distributors}
    statement_id_for = {
        feed_key: (absolute_period_index + 1) * 100 + dist_id
        for feed_key, dist_id in dist_by_feed.items()
    }

    release_by_id = {r.id: r for r in world.releases}
    period_end = period_end_date(period)
    # Native unit prices, precomputed once per (store, territory, currency).
    price_native: dict[tuple[str, str, str], Decimal] = {}
    for (store_name, territory), usd in cells.price_usd.items():
        for currency in ("USD", "EUR", "GBP", "JPY"):
            price_native[(store_name, territory, currency)] = native_price(usd, currency, fx)

    lines: list[StatementLine] = []
    seq = 0

    def next_id() -> int:
        nonlocal seq
        seq += 1
        return (absolute_period_index + 1) * 10_000_000 + seq

    def emit(
        feed_key: str,
        store: str,
        isrc: str,
        upc: str | None,
        territory: str,
        units: int,
        price: Decimal,
        currency: str,
    ) -> None:
        gross = line_gross(price, units, currency)
        lines.append(
            StatementLine(
                id=next_id(),
                statement_id=statement_id_for[feed_key],
                period=period,
                isrc=isrc,
                upc=upc,
                store=store,
                territory=territory,
                units=units,
                gross_amount=gross,
                currency=currency,
                line_hash=line_hash(
                    feed_key, period, isrc, upc, store, territory, units, gross, currency
                ),
            )
        )

    # Track-grain cells (streaming + download), stores in config order.
    for tc in cells.track_cells:
        feed = config.feeds[tc.store.feed]
        min_units = config.revenue.min_cell_units[tc.store.revenue_type]
        jitter = pgen.lognormal(mean=0.0, sigma=0.30, size=len(tc.base_units))
        months_since = absolute_period_index - cells.track_origin_pidx[tc.track_pos]
        ramps = np.array([_ramp(int(m), config.revenue.new_release_ramp) for m in months_since])
        units_f = tc.base_units * jitter * seasonal * ramps
        units_i = np.rint(units_f).astype(np.int64)
        for i in np.nonzero(units_i >= min_units)[0]:
            track = world.tracks[int(tc.track_pos[i])]
            territory = territories[int(tc.terr_idx[i])]
            comp_pos = int(tc.comp_release_pos[i])
            if comp_pos >= 0 and world.releases[comp_pos].release_date <= period_end:
                upc = world.releases[comp_pos].upc
            else:
                upc = release_by_id[track.origin_release_id].upc
            currency = feed.currency
            if feed.gbp_territories and territory in feed.gbp_territories:
                currency = "GBP"
            price = price_native[(tc.store.name, territory, currency)]
            emit(
                feed.key,
                tc.store.name,
                track.isrc,
                upc,
                territory,
                int(units_i[i]),
                price,
                currency,
            )

    # Release-grain cells (physical): isrc is blank, matching goes by UPC.
    for rc in cells.release_cells:
        feed = config.feeds[rc.store.feed]
        min_units = config.revenue.min_cell_units["physical"]
        jitter = pgen.lognormal(mean=0.0, sigma=0.35, size=len(rc.base_units))
        rel_dates = [world.releases[int(p)].release_date for p in rc.release_pos]
        start_y, start_m = int(config.start_period[:4]), int(config.start_period[5:7])
        months_since = np.array(
            [
                absolute_period_index - ((d.year * 12 + d.month) - (start_y * 12 + start_m))
                for d in rel_dates
            ]
        )
        ramps = np.array([_ramp(int(m), config.revenue.new_release_ramp) for m in months_since])
        units_f = rc.base_units * jitter * seasonal * ramps
        units_i = np.rint(units_f).astype(np.int64)
        for i in np.nonzero(units_i >= min_units)[0]:
            release = world.releases[int(rc.release_pos[i])]
            territory = territories[int(rc.terr_idx[i])]
            currency = feed.currency
            if feed.gbp_territories and territory in feed.gbp_territories:
                currency = "GBP"
            price = price_native[(rc.store.name, territory, currency)]
            emit(
                feed.key,
                rc.store.name,
                "",
                release.upc,
                territory,
                int(units_i[i]),
                price,
                currency,
            )

    # Sync: sparse, high-value, one placement per line (units = 1).
    sync_stores = [s for s in config.stores if s.revenue_type == "sync"]
    if sync_stores:
        store = sync_stores[0]
        feed = config.feeds[store.feed]
        n_sync = int(
            pgen.integers(
                config.revenue.sync_lines_per_period.lo,
                config.revenue.sync_lines_per_period.hi + 1,
            )
        )
        cutoff = period_end_date(period)
        eligible = [
            (i, t)
            for i, t in enumerate(world.tracks)
            if (cutoff - t.origin_release_date).days >= 60
        ]
        weights = np.array([structure.track_pop[t.id] for _, t in eligible])
        weights = weights / weights.sum()
        sync_territories = ["US", "GB", "DE", "FR", "JP"]
        for _ in range(n_sync):
            _, track = eligible[int(pgen.choice(len(eligible), p=weights))]
            fee = int(
                round(
                    float(
                        np.exp(
                            pgen.uniform(
                                np.log(config.revenue.sync_fee_usd.lo),
                                np.log(config.revenue.sync_fee_usd.hi),
                            )
                        )
                    )
                    / 50
                )
                * 50
            )
            territory = sync_territories[int(pgen.integers(0, len(sync_territories)))]
            upc = release_by_id[track.origin_release_id].upc
            emit(feed.key, store.name, track.isrc, upc, territory, 1, Decimal(fee), "USD")

    return lines


def build_statements(
    structure: Structure, period_indices: list[int], status: str
) -> list[Statement]:
    """One statement row per (feed, period)."""
    config = structure.config
    world = structure.world
    from datagen.feeds import drop_filename  # local import to avoid a cycle

    statements: list[Statement] = []
    for pidx in period_indices:
        period = period_add(config.start_period, pidx)
        for distributor in world.distributors:
            feed = config.feeds[distributor.feed_key]
            received = period_end_date(period) + timedelta(days=feed.received_day)
            statements.append(
                Statement(
                    id=(pidx + 1) * 100 + distributor.id,
                    distributor_id=distributor.id,
                    period=period,
                    received_at=received,
                    raw_path=f"data/inbox/{drop_filename(feed.dialect, period)}",
                    status=status,
                )
            )
    return statements
