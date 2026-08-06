"""Feed-dialect normalizer: six CSV habits → canonical statement lines (§4.3).

The exact inverse of ``datagen/feeds.py``: each dialect's column names, delimiters,
period formats, decimal habits, and quirks (blank ISRCs, missing currency/units
columns) parse back to the canonical value set, quantized through ``money6`` so the
recomputed ``line_hash`` — datagen's own formula, imported, not reimplemented —
reproduces the generator's hashes for untampered lines. Malformed rows are collected
as row-numbered errors, never silently dropped and never fatal to the drop.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from functools import lru_cache

from backline.royaltycalc import money6
from datagen.config import load_world_config
from datagen.revenue import line_hash

_MONTHS = {
    m: i + 1
    for i, m in enumerate(
        ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
    )
}


@dataclass(frozen=True)
class ParsedLine:
    period: str
    isrc: str  # '' for physical (release-level) lines
    upc: str | None
    store: str
    territory: str
    units: int
    gross_amount: Decimal  # money6-quantized, native feed currency
    currency: str
    line_hash: str


@lru_cache
def _dialect_meta() -> dict[str, tuple[str, tuple[str, ...]]]:
    """dialect → (feed_key, store names of that feed) from the world config."""
    config = load_world_config()
    stores_of_feed: dict[str, list[str]] = {}
    for store in config.stores:
        stores_of_feed.setdefault(store.feed, []).append(store.name)
    return {
        feed.dialect: (key, tuple(stores_of_feed.get(key, ())))
        for key, feed in config.feeds.items()
    }


def _period_iso(raw: str) -> str:  # "2026-02"
    year, month = raw.split("-")
    return f"{int(year):04d}-{int(month):02d}"


def _period_de(raw: str) -> str:  # "02.2026"
    month, year = raw.split(".")
    return f"{int(year):04d}-{int(month):02d}"


def _period_monname(raw: str) -> str:  # "Feb-2026"
    mon, year = raw.split("-")
    return f"{int(year):04d}-{_MONTHS[mon]:02d}"


def _period_us_date(raw: str) -> str:  # "02/01/2026" (MM/DD/YYYY)
    month, _day, year = raw.split("/")
    return f"{int(year):04d}-{int(month):02d}"


def _period_jp(raw: str) -> str:  # "2026/02"
    year, month = raw.split("/")
    return f"{int(year):04d}-{int(month):02d}"


def _units(raw: str) -> int:
    return int(raw.strip())


def _amount(raw: str, *, decimal_comma: bool = False) -> Decimal:
    text = raw.strip()
    if decimal_comma:
        text = text.replace(",", ".")
    return money6(Decimal(text))


def _single_store(dialect: str, stores: tuple[str, ...]) -> str:
    if len(stores) != 1:
        raise ValueError(
            f"dialect {dialect!r} has no store column and the world config lists "
            f"{len(stores)} stores for its feed — cannot infer the store"
        )
    return stores[0]


def parse_drop(dialect: str, text: str) -> tuple[list[ParsedLine], list[str]]:
    """Parse one drop. Returns (lines, row-numbered error messages)."""
    meta = _dialect_meta().get(dialect)
    if meta is None:
        raise ValueError(f"unknown feed dialect {dialect!r}")
    feed_key, feed_stores = meta

    delimiter = {"meridian_eu": ";", "vantage_jp": "\t"}.get(dialect, ",")
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    rows = list(reader)
    lines: list[ParsedLine] = []
    errors: list[str] = []

    for row_no, row in enumerate(rows[1:], start=2):  # row 1 is the header
        if not row or all(not cell.strip() for cell in row):
            continue
        parsed: tuple[str, str, str, str, str, int, Decimal, str]
        try:
            if dialect == "kinetic_us":
                period_raw, isrc, upc, store, territory, units, amount, currency = row
                parsed = (
                    _period_iso(period_raw),
                    isrc,
                    upc,
                    store,
                    territory,
                    _units(units),
                    _amount(amount),
                    currency,
                )
            elif dialect == "meridian_eu":
                period_raw, isrc, upc, store, territory, units, amount, currency = row
                parsed = (
                    _period_de(period_raw),
                    isrc,
                    upc,
                    store,
                    territory,
                    _units(units),
                    _amount(amount, decimal_comma=True),
                    currency,
                )
            elif dialect == "pulsewave_uk":
                period_raw, isrc, upc, store, territory, units, amount, currency = row
                parsed = (
                    _period_monname(period_raw),
                    isrc,
                    upc,
                    store,
                    territory,
                    _units(units),
                    _amount(amount),
                    currency,
                )
            elif dialect == "northstar_retail":
                start, _end, upc, isrc, store, territory, units, amount, currency = row
                parsed = (
                    _period_us_date(start),
                    isrc,
                    upc,
                    store,
                    territory,
                    _units(units),
                    _amount(amount),
                    currency,
                )
            elif dialect == "vantage_jp":
                period_raw, isrc, upc, territory, units, amount = row
                parsed = (
                    _period_jp(period_raw),
                    isrc,
                    upc,
                    _single_store(dialect, feed_stores),
                    territory,
                    _units(units),
                    _amount(amount),
                    "JPY",
                )
            elif dialect == "syncbridge_lic":
                period_raw, isrc, upc, _placement, territory, amount, currency = row
                parsed = (
                    _period_iso(period_raw),
                    isrc,
                    upc,
                    _single_store(dialect, feed_stores),
                    territory,
                    1,
                    _amount(amount),
                    currency,
                )
            else:  # pragma: no cover — guarded above
                raise ValueError(f"unknown dialect {dialect!r}")
        except (ValueError, InvalidOperation, KeyError, IndexError) as error:
            errors.append(f"row {row_no}: {type(error).__name__}: {error}")
            continue

        period, isrc_raw, upc_raw, store_name, territory_code, n_units, gross, ccy = parsed
        upc_value = upc_raw.strip() or None
        isrc_value = isrc_raw.strip()
        lines.append(
            ParsedLine(
                period=period,
                isrc=isrc_value,
                upc=upc_value,
                store=store_name,
                territory=territory_code,
                units=n_units,
                gross_amount=gross,
                currency=ccy,
                line_hash=line_hash(
                    feed_key,
                    period,
                    isrc_value,
                    upc_value,
                    store_name,
                    territory_code,
                    n_units,
                    gross,
                    ccy,
                ),
            )
        )
    return lines, errors
