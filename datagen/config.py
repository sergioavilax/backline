"""Typed loader for ``datagen/world.yaml``.

All Decimal quantities (money, rates, FX) are strings in the YAML and become ``Decimal``
here — floats are only permitted for probabilities/weights, which never touch money.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

WORLD_YAML = Path(__file__).resolve().parent / "world.yaml"


@dataclass(frozen=True)
class StoreCfg:
    name: str
    revenue_type: str
    feed: str
    unit_rate_usd: Decimal
    scale: float
    territory_bias: dict[str, float]


@dataclass(frozen=True)
class FeedCfg:
    key: str
    name: str
    dialect: str
    currency: str
    received_day: int
    gbp_territories: tuple[str, ...]


@dataclass(frozen=True)
class Range:
    lo: int
    hi: int


@dataclass(frozen=True)
class RevenueCfg:
    base_monthly_units: dict[str, float]
    activation: dict[str, float]
    min_cell_units: dict[str, int]
    physical_release_share: float
    seasonal: dict[str, float]  # month "01".."12" -> multiplier
    new_release_ramp: tuple[float, ...]
    compilation_upc_share: float
    sync_lines_per_period: Range
    sync_fee_usd: Range


@dataclass(frozen=True)
class ContractsCfg:
    deals_per_artist_probs: tuple[float, ...]
    amendment_share: float
    amendment_in_window_share: float
    streaming_rate_pts: Range
    download_rate_pts: Range
    sync_rate_pts: Range
    sync_coverage: float
    physical_gb_rate_pts: Range
    physical_ww_rate_pts: Range
    physical_gb_override_share: float
    territory_override_share: float
    escalator_share: float
    escalator_thresholds_usd: tuple[Decimal, ...]
    escalator_bump_pts: tuple[int, ...]
    minimum_guarantee_usd: Decimal
    recoupable_classes_full: tuple[str, ...]
    recoupable_classes_no_tour_share: float
    advance_usd: Range
    expense_usd: Range
    expense_classes: tuple[str, ...]
    opening_balance_zero_share: float
    opening_balance_usd: Range


@dataclass(frozen=True)
class AnomalyPlanCfg:
    counts: dict[str, int]  # kind -> count (flaggable kinds only)
    borderline_dashboard_gap: int
    borderline_territory_spike: int
    dashboard_gap_tolerance_pct: float
    emitted_period_anomalies: int


@dataclass(frozen=True)
class NamePools:
    given: tuple[str, ...]
    surname: tuple[str, ...]
    stage_adjectives: tuple[str, ...]
    stage_nouns: tuple[str, ...]
    title_a: tuple[str, ...]
    title_b: tuple[str, ...]
    places: tuple[str, ...]
    sync_placements: tuple[str, ...]


@dataclass(frozen=True)
class WorldConfig:
    label_name: str
    imprints: tuple[str, ...]
    start_period: str
    n_periods: int
    n_artists: int
    n_xcollat_artists: int
    n_compilations: int
    target_advances: int
    target_expenses: int
    fx_rates: dict[str, dict[str, Decimal]]  # period -> currency -> USD per unit
    territories: tuple[str, ...]
    territory_weights: dict[str, float]
    stores: tuple[StoreCfg, ...]
    feeds: dict[str, FeedCfg]
    revenue: RevenueCfg
    contracts: ContractsCfg
    anomalies: AnomalyPlanCfg
    canary_text: str
    pools: NamePools

    @property
    def periods(self) -> tuple[str, ...]:
        """The seeded statement periods, "YYYY-MM", in order."""
        return tuple(period_add(self.start_period, i) for i in range(self.n_periods))


def period_add(period: str, months: int) -> str:
    y, m = int(period[:4]), int(period[5:7])
    total = y * 12 + (m - 1) + months
    return f"{total // 12:04d}-{total % 12 + 1:02d}"


def period_index(start_period: str, period: str) -> int:
    y0, m0 = int(start_period[:4]), int(start_period[5:7])
    y, m = int(period[:4]), int(period[5:7])
    return (y * 12 + m) - (y0 * 12 + m0)


def period_start_date(period: str) -> date:
    return date(int(period[:4]), int(period[5:7]), 1)


def period_end_date(period: str) -> date:
    return period_start_date(period_add(period, 1)) - timedelta(days=1)


def _dec(value: Any) -> Decimal:
    if isinstance(value, float):
        raise TypeError(f"world.yaml decimal quantities must be strings, got float {value!r}")
    return Decimal(str(value))


def _range(raw: dict[str, Any]) -> Range:
    return Range(lo=int(raw["lo"]), hi=int(raw["hi"]))


def load_world_config(path: Path = WORLD_YAML) -> WorldConfig:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))

    w = raw["world"]
    fx = {
        period: {ccy: _dec(rate) for ccy, rate in table.items()}
        for period, table in raw["fx_rates"].items()
    }
    stores = tuple(
        StoreCfg(
            name=s["name"],
            revenue_type=s["revenue_type"],
            feed=s["feed"],
            unit_rate_usd=_dec(s["unit_rate_usd"]),
            scale=float(s["scale"]),
            territory_bias={k: float(v) for k, v in s.get("territory_bias", {}).items()},
        )
        for s in raw["stores"]
    )
    feeds = {
        key: FeedCfg(
            key=key,
            name=f["name"],
            dialect=f["dialect"],
            currency=f["currency"],
            received_day=int(f["received_day"]),
            gbp_territories=tuple(f.get("gbp_territories", [])),
        )
        for key, f in raw["feeds"].items()
    }
    rev = raw["revenue"]
    revenue = RevenueCfg(
        base_monthly_units={k: float(v) for k, v in rev["base_monthly_units"].items()},
        activation={k: float(v) for k, v in rev["activation"].items()},
        min_cell_units={k: int(v) for k, v in rev["min_cell_units"].items()},
        physical_release_share=float(rev["physical_release_share"]),
        seasonal={k: float(v) for k, v in rev["seasonal"].items()},
        new_release_ramp=tuple(float(x) for x in rev["new_release_ramp"]),
        compilation_upc_share=float(rev["compilation_upc_share"]),
        sync_lines_per_period=Range(
            lo=int(rev["sync_lines_per_period_lo"]), hi=int(rev["sync_lines_per_period_hi"])
        ),
        sync_fee_usd=Range(lo=int(rev["sync_fee_usd_lo"]), hi=int(rev["sync_fee_usd_hi"])),
    )
    c = raw["contracts"]
    contracts = ContractsCfg(
        deals_per_artist_probs=tuple(float(p) for p in c["deals_per_artist_probs"]),
        amendment_share=float(c["amendment_share"]),
        amendment_in_window_share=float(c["amendment_in_window_share"]),
        streaming_rate_pts=_range(c["streaming_rate_pts"]),
        download_rate_pts=_range(c["download_rate_pts"]),
        sync_rate_pts=_range(c["sync_rate_pts"]),
        sync_coverage=float(c["sync_coverage"]),
        physical_gb_rate_pts=_range(c["physical_gb_rate_pts"]),
        physical_ww_rate_pts=_range(c["physical_ww_rate_pts"]),
        physical_gb_override_share=float(c["physical_gb_override_share"]),
        territory_override_share=float(c["territory_override_share"]),
        escalator_share=float(c["escalator_share"]),
        escalator_thresholds_usd=tuple(_dec(t) for t in c["escalator_thresholds_usd"]),
        escalator_bump_pts=tuple(int(b) for b in c["escalator_bump_pts"]),
        minimum_guarantee_usd=_dec(c["minimum_guarantee_usd"]),
        recoupable_classes_full=tuple(c["recoupable_classes_full"]),
        recoupable_classes_no_tour_share=float(c["recoupable_classes_no_tour_share"]),
        advance_usd=_range(c["advance_usd"]),
        expense_usd=_range(c["expense_usd"]),
        expense_classes=tuple(c["expense_classes"]),
        opening_balance_zero_share=float(c["opening_balance_zero_share"]),
        opening_balance_usd=_range(c["opening_balance_usd"]),
    )
    a = raw["anomalies"]
    flaggable = {
        kind: int(a[kind])
        for kind in (
            "duplicate_line",
            "unknown_isrc",
            "currency_mismatch",
            "negative_units",
            "dashboard_gap",
            "period_bleed",
            "sudden_territory_spike",
        )
    }
    anomalies = AnomalyPlanCfg(
        counts=flaggable,
        borderline_dashboard_gap=int(a["borderline_dashboard_gap"]),
        borderline_territory_spike=int(a["borderline_territory_spike"]),
        dashboard_gap_tolerance_pct=float(a["dashboard_gap_tolerance_pct"]),
        emitted_period_anomalies=int(a["emitted_period_anomalies"]),
    )
    p = raw["name_pools"]
    pools = NamePools(
        given=tuple(p["given"]),
        surname=tuple(p["surname"]),
        stage_adjectives=tuple(p["stage_adjectives"]),
        stage_nouns=tuple(p["stage_nouns"]),
        title_a=tuple(p["title_a"]),
        title_b=tuple(p["title_b"]),
        places=tuple(p["places"]),
        sync_placements=tuple(p["sync_placements"]),
    )
    return WorldConfig(
        label_name=w["label_name"],
        imprints=tuple(w["imprints"]),
        start_period=str(w["start_period"]),
        n_periods=int(w["n_periods"]),
        n_artists=int(w["n_artists"]),
        n_xcollat_artists=int(w["n_xcollat_artists"]),
        n_compilations=int(w["n_compilations"]),
        target_advances=int(w["target_advances"]),
        target_expenses=int(w["target_expenses"]),
        fx_rates=fx,
        territories=tuple(raw["territories"]),
        territory_weights={k: float(v) for k, v in raw["territory_weights"].items()},
        stores=stores,
        feeds=feeds,
        revenue=revenue,
        contracts=contracts,
        anomalies=anomalies,
        canary_text=str(raw["canary_text"]).strip(),
        pools=pools,
    )
