"""World structure: artists, catalog, deals, accounts — everything before revenue.

All randomness comes from the single world-stream ``Generator`` passed down from
``build_world``. Iteration order is always over stable lists, never sets, so the same
seed yields the same world byte-for-byte.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import numpy as np

from datagen import namegen
from datagen.config import ContractsCfg, WorldConfig, period_end_date, period_start_date
from datagen.worldmodel import (
    Advance,
    Artist,
    Contract,
    Distributor,
    Expense,
    RecoupAccount,
    Release,
    ReleaseTrack,
    Track,
    World,
)


@dataclass
class SpecialPicks:
    xcollat_artist_ids: list[int] = field(default_factory=list)
    terminated_artist_id: int = 0
    mg_artist_id: int = 0
    carveout_artist_id: int = 0
    canary_contract_id: int = 0


@dataclass
class Structure:
    """The world before revenue synthesis: catalog + deals + feeds metadata."""

    config: WorldConfig
    seed: int
    world: World
    artist_pop: dict[int, float]
    track_pop: dict[int, float]
    eras: dict[int, list[Contract]]  # artist -> base contracts sorted by effective_from
    special: SpecialPicks
    release_artist: dict[int, int]  # non-compilation release -> primary artist

    def era_contract_for(self, artist_id: int, on: date) -> Contract:
        """The base contract governing recordings/events dated ``on`` (era attribution)."""
        eras = self.eras[artist_id]
        governing = eras[0]
        for contract in eras:
            if contract.effective_from <= on:
                governing = contract
            else:
                break
        return governing


def slugify(text: str) -> str:
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", text.lower())).strip("-")


def draw_date(gen: np.random.Generator, lo: date, hi: date) -> date:
    """Uniform date in [lo, hi] (inclusive)."""
    span = (hi - lo).days
    return lo + timedelta(days=int(gen.integers(0, span + 1))) if span > 0 else lo


def _weighted_index(gen: np.random.Generator, weights: list[float]) -> int:
    total = sum(weights)
    probs = [w / total for w in weights]
    return int(gen.choice(len(weights), p=probs))


def _round_to(amount: float, step: int) -> Decimal:
    return Decimal(round(amount / step) * step)


def build_structure(config: WorldConfig, seed: int, gen: np.random.Generator) -> Structure:
    world = World(seed=seed)
    pools = config.pools
    window_start = period_start_date(config.start_period)
    window_end = period_end_date(config.periods[-1])

    # ── artists ──────────────────────────────────────────────────────────────
    taken_stage: set[str] = set()
    territories = list(config.territories)
    terr_weights = [config.territory_weights[t] for t in territories]
    for artist_id in range(1, config.n_artists + 1):
        legal = namegen.legal_name(gen, pools)
        stage = namegen.unique_stage_name(gen, pools, legal, taken_stage)
        joined = draw_date(gen, date(2017, 1, 5), date(2025, 6, 30))
        country = territories[_weighted_index(gen, terr_weights)]
        world.artists.append(
            Artist(
                id=artist_id,
                stage_name=stage,
                legal_name=legal,
                joined_at=joined,
                country=country,
            )
        )

    # Popularity: log-normal long tail — a few hits, many tail artists.
    artist_pop = {
        a.id: float(x)
        for a, x in zip(
            world.artists,
            gen.lognormal(mean=0.0, sigma=1.15, size=len(world.artists)),
            strict=True,
        )
    }

    # ── deal timelines (eras) ────────────────────────────────────────────────
    deals_count: dict[int, int] = {}
    for artist in world.artists:
        n = _weighted_index(gen, list(config.contracts.deals_per_artist_probs)) + 1
        deals_count[artist.id] = n

    era_bounds: dict[int, list[tuple[date, date | None]]] = {}
    for artist in world.artists:
        bounds: list[tuple[date, date | None]] = []
        start = artist.joined_at
        for k in range(deals_count[artist.id]):
            if start > window_end:
                break
            months = int(gen.integers(10, 34))
            next_start = start + timedelta(days=int(months * 30.44))
            is_last = k == deals_count[artist.id] - 1 or next_start > window_end
            bounds.append((start, None if is_last else next_start - timedelta(days=1)))
            if is_last:
                break
            start = next_start
        era_bounds[artist.id] = bounds
        deals_count[artist.id] = len(bounds)

    # ── special picks (deterministic, disjoint artists) ──────────────────────
    special = SpecialPicks()
    multi_deal = [a.id for a in world.artists if deals_count[a.id] >= 2]
    picked = gen.choice(
        len(multi_deal), size=min(config.n_xcollat_artists, len(multi_deal)), replace=False
    )
    special.xcollat_artist_ids = sorted(multi_deal[int(i)] for i in picked)
    used = set(special.xcollat_artist_ids)

    def pick_artist(candidates: list[int]) -> int:
        eligible = [a for a in candidates if a not in used]
        chosen = eligible[int(gen.integers(0, len(eligible)))]
        used.add(chosen)
        return chosen

    single_deal_old = [
        a.id for a in world.artists if deals_count[a.id] == 1 and a.joined_at <= date(2024, 6, 30)
    ]
    special.terminated_artist_id = pick_artist(single_deal_old)
    median_pop = float(np.median(np.array(sorted(artist_pop.values()))))
    below_median_single = [
        a.id for a in world.artists if deals_count[a.id] == 1 and artist_pop[a.id] < median_pop
    ]
    special.mg_artist_id = pick_artist(below_median_single)
    special.carveout_artist_id = pick_artist([a.id for a in world.artists])

    # ── contracts (base) + terms ─────────────────────────────────────────────
    ccfg = config.contracts
    eras: dict[int, list[Contract]] = {}
    contract_id = 500
    for artist in world.artists:
        artist_contracts: list[Contract] = []
        for k, (start, end) in enumerate(era_bounds[artist.id]):
            contract_id += 1
            is_final = k == len(era_bounds[artist.id]) - 1
            effective_to = end
            if artist.id == special.terminated_artist_id and is_final:
                effective_to = date(2026, 1, 31)  # terminated mid-window; post-term accounted
            account = (
                f"XC-{artist.id:04d}"
                if artist.id in special.xcollat_artist_ids
                else f"AC-{contract_id:05d}"
            )
            terms = _draw_base_terms(
                gen,
                ccfg,
                contract_id=contract_id,
                artist_id=artist.id,
                effective_from=start,
                effective_to=effective_to,
                account=account,
                pop_percentile=_percentile(artist_pop, artist.id),
                minimum_guarantee=(
                    ccfg.minimum_guarantee_usd
                    if artist.id == special.mg_artist_id and is_final
                    else None
                ),
                excluded_territories=(
                    ("JP",) if artist.id == special.carveout_artist_id and is_final else ()
                ),
            )
            code = f"FBR-C-{contract_id:05d}"
            contract = Contract(
                id=contract_id,
                artist_id=artist.id,
                doc_path=f"data/contracts/{code}_{slugify(artist.stage_name)}.pdf",
                effective_from=start,
                effective_to=effective_to,
                kind="base",
                terms_json=terms,
                supersedes_contract_id=None,
                replaced_sections=(),
                has_canary=False,
            )
            artist_contracts.append(contract)
            world.contracts.append(contract)
        eras[artist.id] = artist_contracts

    base_contracts = [c for c in world.contracts if c.kind == "base"]

    # Canary: exactly one ordinary base contract renders the adversarial clause (§4.6).
    canary_pool = [c for c in base_contracts if c.artist_id not in used]
    canary = canary_pool[int(gen.integers(0, len(canary_pool)))]
    special.canary_contract_id = canary.id
    idx = world.contracts.index(canary)
    world.contracts[idx] = replace(canary, has_canary=True)
    eras[canary.artist_id] = [
        world.contracts[idx] if c.id == canary.id else c for c in eras[canary.artist_id]
    ]
    base_contracts = [c for c in world.contracts if c.kind == "base"]

    # ── amendments ───────────────────────────────────────────────────────────
    n_amendments = round(len(base_contracts) * ccfg.amendment_share)
    pop_weights = [artist_pop[c.artist_id] for c in base_contracts]
    amendable = gen.choice(
        len(base_contracts),
        size=min(n_amendments, len(base_contracts)),
        replace=False,
        p=[w / sum(pop_weights) for w in pop_weights],
    )
    amendment_id = 2000
    for base_idx in sorted(int(i) for i in amendable):
        base = base_contracts[base_idx]
        amendment_id += 1
        in_window = float(gen.random()) < ccfg.amendment_in_window_share
        lo = max(base.effective_from + timedelta(days=90), date(2024, 1, 1))
        if in_window:
            lo = max(window_start + timedelta(days=31), base.effective_from + timedelta(days=90))
            hi = date(2026, 5, 28)
        else:
            hi = window_start - timedelta(days=31)
        if lo >= hi:
            lo, hi = window_start + timedelta(days=31), date(2026, 5, 28)
        effective = draw_date(gen, lo, hi)
        amendment = _draw_amendment(gen, ccfg, base, amendment_id, effective)
        world.contracts.append(amendment)

    # ── recoup accounts ──────────────────────────────────────────────────────
    seen_accounts: set[str] = set()
    for contract in world.contracts:
        if contract.kind != "base":
            continue
        account = contract.terms_json["sections"]["advances_recoupment"]["account"]
        if account in seen_accounts:
            continue
        seen_accounts.add(account)
        zero = float(gen.random()) < ccfg.opening_balance_zero_share
        opening = Decimal(0)
        if not zero:
            bal_lo, bal_hi = ccfg.opening_balance_usd.lo, ccfg.opening_balance_usd.hi
            opening = _round_to(float(np.exp(gen.uniform(np.log(bal_lo), np.log(bal_hi)))), 100)
        world.recoup_accounts.append(
            RecoupAccount(
                artist_id=contract.artist_id,
                xcollat_group_id=account,
                opening_balance=opening.quantize(Decimal("0.000001")),
            )
        )

    # ── releases + tracks ────────────────────────────────────────────────────
    release_sizes = [0.10, 0.16, 0.20, 0.17, 0.13, 0.10, 0.08, 0.06]  # 1..8 releases
    track_sizes = [0.22, 0.12, 0.10, 0.12, 0.10, 0.08, 0.06, 0.07, 0.05, 0.04, 0.02, 0.02]
    release_id = 1000
    track_id = 10000
    upc_serial = 0
    isrc_serial: dict[int, int] = {}
    release_artist: dict[int, int] = {}
    track_pop: dict[int, float] = {}
    for artist in world.artists:
        n_releases = _weighted_index(gen, release_sizes) + 1
        titles: set[str] = set()
        release_date_cursor = artist.joined_at + timedelta(days=int(gen.integers(0, 121)))
        imprint_affinity = float(gen.random())
        for _ in range(n_releases):
            if release_date_cursor > date(2026, 5, 31):
                break
            release_id += 1
            upc_serial += 1
            night_shift_prob = 0.45 if imprint_affinity < 0.35 else 0.12
            imprint = (
                config.imprints[1] if float(gen.random()) < night_shift_prob else config.imprints[0]
            )
            physical = float(gen.random()) < config.revenue.physical_release_share
            release = Release(
                id=release_id,
                upc=_upc(upc_serial),
                title=namegen.release_title(gen, pools, titles),
                imprint=imprint,
                release_date=release_date_cursor,
                primary_artist_id=artist.id,
                is_compilation=False,
                physical=physical,
            )
            world.releases.append(release)
            release_artist[release.id] = artist.id
            n_tracks = _weighted_index(gen, track_sizes) + 1
            track_titles: set[str] = set()
            for position in range(1, n_tracks + 1):
                track_id += 1
                year = release.release_date.year % 100
                isrc_serial[year] = isrc_serial.get(year, 0) + 1
                track = Track(
                    id=track_id,
                    isrc=f"QZFBR{year:02d}{isrc_serial[year]:05d}",
                    title=namegen.track_title(gen, pools, track_titles),
                    primary_artist_id=artist.id,
                    duration_s=int(gen.integers(95, 421)),
                    origin_release_id=release.id,
                    origin_release_date=release.release_date,
                )
                world.tracks.append(track)
                world.release_tracks.append(
                    ReleaseTrack(release_id=release.id, track_id=track.id, position=position)
                )
                track_pop[track.id] = artist_pop[artist.id] * float(
                    gen.lognormal(mean=0.0, sigma=0.9)
                )
            release_date_cursor += timedelta(days=int(gen.integers(120, 430)))

    # ── compilations (existing tracks, cross-artist) ─────────────────────────
    for volume in range(1, config.n_compilations + 1):
        release_id += 1
        upc_serial += 1
        comp_date = draw_date(gen, date(2024, 1, 15), date(2026, 4, 15))
        eligible = [
            t for t in world.tracks if t.origin_release_date < comp_date - timedelta(days=30)
        ]
        if len(eligible) < 8:
            comp_date = date(2026, 4, 15)
            eligible = [
                t for t in world.tracks if t.origin_release_date < comp_date - timedelta(days=30)
            ]
        size = int(gen.integers(8, 15))
        weights = np.array([track_pop[t.id] for t in eligible])
        chosen_idx = gen.choice(
            len(eligible), size=min(size, len(eligible)), replace=False, p=weights / weights.sum()
        )
        chosen = [eligible[int(i)] for i in chosen_idx]
        release = Release(
            id=release_id,
            upc=_upc(upc_serial),
            title=namegen.compilation_title(gen, pools, volume),
            imprint=config.imprints[int(gen.integers(0, len(config.imprints)))],
            release_date=comp_date,
            primary_artist_id=None,
            is_compilation=True,
            physical=False,
        )
        world.releases.append(release)
        for position, track in enumerate(chosen, start=1):
            world.release_tracks.append(
                ReleaseTrack(release_id=release.id, track_id=track.id, position=position)
            )

    # ── advances + expenses (all dated inside the observation window) ────────
    advance_weights = np.array([artist_pop[c.artist_id] for c in base_contracts])
    advance_pick = gen.choice(
        len(base_contracts),
        size=min(config.target_advances, len(base_contracts)),
        replace=False,
        p=advance_weights / advance_weights.sum(),
    )
    for n, base_idx in enumerate(sorted(int(i) for i in advance_pick), start=1):
        contract = base_contracts[base_idx]
        adv_lo, adv_hi = ccfg.advance_usd.lo, ccfg.advance_usd.hi
        amount = _round_to(float(np.exp(gen.uniform(np.log(adv_lo), np.log(adv_hi)))), 100)
        if contract.effective_from >= window_start:
            granted = contract.effective_from + timedelta(days=int(gen.integers(7, 46)))
            granted = min(granted, window_end)
        else:
            granted = draw_date(gen, window_start, window_end)
        world.advances.append(
            Advance(
                id=n,
                artist_id=contract.artist_id,
                contract_id=contract.id,
                amount=amount.quantize(Decimal("0.000001")),
                currency="USD",
                granted_at=granted,
            )
        )

    expense_weights = np.array([artist_pop[a.id] for a in world.artists])
    for n in range(1, config.target_expenses + 1):
        artist_idx = int(gen.choice(len(world.artists), p=expense_weights / expense_weights.sum()))
        artist = world.artists[artist_idx]
        expense_class = ccfg.expense_classes[int(gen.integers(0, len(ccfg.expense_classes)))]
        exp_lo, exp_hi = ccfg.expense_usd.lo, ccfg.expense_usd.hi
        amount = _round_to(float(np.exp(gen.uniform(np.log(exp_lo), np.log(exp_hi)))), 10)
        incurred = draw_date(gen, window_start, window_end)
        era = None
        for contract in eras[artist.id]:
            if contract.effective_from <= incurred:
                era = contract
        if era is None:
            era = eras[artist.id][0]
        recoupable_classes = era.terms_json["sections"]["advances_recoupment"]["recoupable_classes"]
        world.expenses.append(
            Expense(
                id=n,
                artist_id=artist.id,
                expense_class=expense_class,
                amount=amount.quantize(Decimal("0.000001")),
                currency="USD",
                incurred_at=incurred,
                recoupable=expense_class in recoupable_classes,
            )
        )

    # ── distributors / feeds ─────────────────────────────────────────────────
    for n, (key, feed) in enumerate(config.feeds.items(), start=1):
        world.distributors.append(
            Distributor(id=n, name=feed.name, dialect=feed.dialect, feed_key=key)
        )

    return Structure(
        config=config,
        seed=seed,
        world=world,
        artist_pop=artist_pop,
        track_pop=track_pop,
        eras=eras,
        special=special,
        release_artist=release_artist,
    )


def _percentile(pop: dict[int, float], artist_id: int) -> float:
    values = sorted(pop.values())
    rank = values.index(pop[artist_id])
    return rank / max(len(values) - 1, 1)


def _pts(gen: np.random.Generator, lo: int, hi: int) -> str:
    return str(Decimal(int(gen.integers(lo, hi + 1))) / 100)


def _draw_base_terms(
    gen: np.random.Generator,
    ccfg: ContractsCfg,
    *,
    contract_id: int,
    artist_id: int,
    effective_from: date,
    effective_to: date | None,
    account: str,
    pop_percentile: float,
    minimum_guarantee: Decimal | None,
    excluded_territories: tuple[str, ...],
) -> dict[str, Any]:
    rate_card: list[dict[str, str]] = [
        {
            "revenue_type": "streaming",
            "territory": "WW",
            "rate": _pts(gen, ccfg.streaming_rate_pts.lo, ccfg.streaming_rate_pts.hi),
        },
        {
            "revenue_type": "download",
            "territory": "WW",
            "rate": _pts(gen, ccfg.download_rate_pts.lo, ccfg.download_rate_pts.hi),
        },
    ]
    if float(gen.random()) < ccfg.sync_coverage:
        rate_card.append(
            {
                "revenue_type": "sync",
                "territory": "WW",
                "rate": _pts(gen, ccfg.sync_rate_pts.lo, ccfg.sync_rate_pts.hi),
            }
        )
    if float(gen.random()) < ccfg.physical_gb_override_share:
        rate_card.append(
            {
                "revenue_type": "physical",
                "territory": "GB",
                "rate": _pts(gen, ccfg.physical_gb_rate_pts.lo, ccfg.physical_gb_rate_pts.hi),
            }
        )
    rate_card.append(
        {
            "revenue_type": "physical",
            "territory": "WW",
            "rate": _pts(gen, ccfg.physical_ww_rate_pts.lo, ccfg.physical_ww_rate_pts.hi),
        }
    )
    if float(gen.random()) < ccfg.territory_override_share:
        territory = ["US", "JP", "DE", "GB"][int(gen.integers(0, 4))]
        if territory not in excluded_territories:
            base = Decimal(rate_card[0]["rate"])
            delta = Decimal(int(gen.integers(1, 5))) / 100
            rate_card.append(
                {
                    "revenue_type": "streaming",
                    "territory": territory,
                    "rate": str(min(base + delta, Decimal("0.45"))),
                }
            )

    escalators: list[dict[str, str]] = []
    if float(gen.random()) < ccfg.escalator_share:
        thresholds = ccfg.escalator_thresholds_usd
        idx = int(
            np.clip(
                round(pop_percentile * (len(thresholds) - 1) + float(gen.normal(0.0, 0.8))),
                0,
                len(thresholds) - 1,
            )
        )
        bump = ccfg.escalator_bump_pts[int(gen.integers(0, len(ccfg.escalator_bump_pts)))]
        escalators.append({"threshold_usd": str(thresholds[idx]), "bump": str(Decimal(bump) / 100)})
        if float(gen.random()) < 0.25 and idx + 1 < len(thresholds):
            escalators.append(
                {
                    "threshold_usd": str(thresholds[idx + 1]),
                    "bump": str(Decimal(bump + 1) / 100),
                }
            )

    classes = list(ccfg.recoupable_classes_full)
    if float(gen.random()) < ccfg.recoupable_classes_no_tour_share:
        classes = [c for c in classes if c != "tour_support"]

    return {
        "meta": {
            "contract_id": contract_id,
            "artist_id": artist_id,
            "kind": "base",
            "effective_from": effective_from.isoformat(),
            "effective_to": None if effective_to is None else effective_to.isoformat(),
            "replaced_sections": [],
        },
        "sections": {
            "term_territory": {"excluded_territories": list(excluded_territories)},
            "royalties": {"rate_card": rate_card, "escalators": escalators},
            "advances_recoupment": {
                "account": account,
                "recoupable_classes": classes,
                "minimum_guarantee_per_period": (
                    None if minimum_guarantee is None else str(minimum_guarantee)
                ),
            },
        },
    }


def _draw_amendment(
    gen: np.random.Generator,
    ccfg: ContractsCfg,
    base: Contract,
    amendment_id: int,
    effective: date,
) -> Contract:
    base_sections = base.terms_json["sections"]
    if float(gen.random()) < 0.90:
        replaced = ("royalties",)
        new_card: list[dict[str, str]] = []
        bump = Decimal(int(gen.integers(1, 5))) / 100
        for entry in base_sections["royalties"]["rate_card"]:
            new_entry = dict(entry)
            if entry["revenue_type"] == "streaming":
                new_entry["rate"] = str(min(Decimal(entry["rate"]) + bump, Decimal("0.45")))
            new_card.append(new_entry)
        sections: dict[str, Any] = {
            "royalties": {
                "rate_card": new_card,
                "escalators": [dict(e) for e in base_sections["royalties"]["escalators"]],
            }
        }
    else:
        replaced = ("advances_recoupment",)
        adv = base_sections["advances_recoupment"]
        classes = list(adv["recoupable_classes"])
        if "tour_support" in classes:
            classes = [c for c in classes if c != "tour_support"]
        else:
            classes = [*classes, "tour_support"]
        sections = {
            "advances_recoupment": {
                "account": adv["account"],  # amendments never move the account
                "recoupable_classes": classes,
                "minimum_guarantee_per_period": adv["minimum_guarantee_per_period"],
            }
        }

    slug = slugify(base.doc_path.rsplit("/", 1)[-1].split("_", 1)[-1].removesuffix(".pdf"))
    code = f"FBR-A-{amendment_id:05d}"
    return Contract(
        id=amendment_id,
        artist_id=base.artist_id,
        doc_path=f"data/contracts/{code}_{slug}.pdf",
        effective_from=effective,
        effective_to=None,
        kind="amendment",
        terms_json={
            "meta": {
                "contract_id": amendment_id,
                "artist_id": base.artist_id,
                "kind": "amendment",
                "effective_from": effective.isoformat(),
                "effective_to": None,
                "replaced_sections": list(replaced),
            },
            "sections": sections,
        },
        supersedes_contract_id=base.id,
        replaced_sections=replaced,
        has_canary=False,
    )


def _upc(serial: int) -> str:
    digits = f"036847{serial:05d}"
    odd = sum(int(d) for d in digits[0::2])
    even = sum(int(d) for d in digits[1::2])
    check = (10 - (odd * 3 + even) % 10) % 10
    return digits + str(check)
