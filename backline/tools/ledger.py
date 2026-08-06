"""DB → ``royaltycalc`` assembly for the runtime calculator (§4.3, D-001).

This module rebuilds, from Postgres, exactly the inputs the datagen truth engine built
in memory — attribution (ISRC → track → era contract by origin release date; blank-ISRC
→ UPC → single-artist release), terms resolution as of each period's last day,
advances/expenses as period charges, opening balances, fixed monthly FX — and runs the
one royalty engine over them. For an artist whose reported lines carry no corruption,
the result matches ``truth.expected_ledger`` to the microdollar (pinned by test).

Two consumers:

- ``compute_ledger_slice`` — the full recoupment chain from world start through a
  target period, over *reported* lines (optionally minus the agent's exclusions, plus
  staged lines from ``staging.ingested_lines``). Structurally invalid lines (negative
  amounts / non-positive units) are auto-excluded and reported, never silently eaten.
- ``compute_spot_quote`` — rate resolution + application for hypothetical revenue rows
  as of a date, with real escalator state (cumulative gross from the ledger chain).
  Pre-recoupment by construction, and labeled as such.

Store → revenue-type classification comes from the same world config the feeds were
generated from (``datagen/world.yaml``) — the runtime's store reference, exactly like a
real label keeps one alongside its distributor feeds (D-010).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from functools import lru_cache

import asyncpg

from backline.royaltycalc import (
    ArtistState,
    PeriodOutcome,
    RevenueLine,
    Terms,
    TermsDoc,
    compute_artist_period,
    effective_rate,
    escalator_bump,
    money6,
    parse_terms_doc,
    resolve_terms,
    to_usd,
)
from datagen.config import load_world_config, period_end_date

ZERO = Decimal("0")


@lru_cache
def store_revenue_types() -> dict[str, str]:
    """Store name → revenue type, from the world config (the label's store reference)."""
    return {store.name: store.revenue_type for store in load_world_config().stores}


@dataclass(frozen=True)
class _SourceLine:
    id: int
    period: str
    isrc: str
    upc: str | None
    store: str
    territory: str
    units: int
    gross_amount: Decimal
    currency: str
    staged: bool


@dataclass(frozen=True)
class _ArtistWorld:
    """Everything the engine needs for one artist, straight from label.*"""

    artist_id: int
    bases: tuple[TermsDoc, ...]  # sorted by (effective_from, id)
    amendments_of: dict[int, tuple[TermsDoc, ...]]
    fx: dict[str, dict[str, Decimal]]  # period -> currency -> rate
    periods: tuple[str, ...]  # all fx periods, sorted
    isrc_origin: dict[str, date]
    upc_release: dict[str, date]
    opening_balances: dict[str, Decimal]
    advances: tuple[tuple[int, Decimal, date], ...]  # (contract_id, amount, granted_at)
    expenses: tuple[tuple[Decimal, date], ...]  # recoupable only: (amount, incurred_at)


@dataclass(frozen=True)
class LedgerSlice:
    artist_id: int
    period: str
    start_period: str
    gross: Decimal
    recouped: Decimal
    mg_topup: Decimal
    net_payable: Decimal  # cents, half-even — the artist-facing figure
    balance_after: Decimal
    outcome: PeriodOutcome
    by_revenue_type: dict[str, tuple[int, Decimal]]  # type -> (n lines, royalty)
    contracts_used: tuple[int, ...]
    n_lines_used: int
    n_staged_used: int
    excluded_line_ids: tuple[int, ...]  # requested exclusions that matched label lines
    excluded_staged_line_ids: tuple[int, ...]  # requested exclusions that matched staged lines
    auto_excluded_line_ids: tuple[int, ...]  # negative/zero-unit or negative-amount lines


@dataclass(frozen=True)
class SpotLine:
    revenue_type: str
    territory: str
    amount: Decimal
    currency: str
    usd_amount: Decimal
    rate: Decimal
    royalty: Decimal


@dataclass(frozen=True)
class SpotQuote:
    artist_id: int
    contract_id: int
    as_of: date
    fx_period: str
    cumulative_gross_usd: Decimal  # escalator state at the containing period's start
    active_bump: Decimal
    lines: tuple[SpotLine, ...]
    total_royalty: Decimal  # 6dp, pre-recoupment


class CalcInputError(ValueError):
    """Bad calculator input (unknown period, no deals, unknown store...)."""


def _era_for(bases: tuple[TermsDoc, ...], on: date) -> TermsDoc:
    governing = bases[0]
    for base in bases:
        if base.effective_from <= on:
            governing = base
        else:
            break
    return governing


async def _load_artist_world(
    source: asyncpg.Pool | asyncpg.Connection, artist_id: int
) -> _ArtistWorld:
    contract_rows = await source.fetch(
        """
        SELECT c.id, c.kind, ct.terms, a.supersedes_contract_id
        FROM label.contracts c
        JOIN label.contract_terms ct ON ct.contract_id = c.id
        LEFT JOIN label.amendments a ON a.amendment_id = c.id
        WHERE c.artist_id = $1
        ORDER BY c.id
        """,
        artist_id,
    )
    if not contract_rows:
        raise CalcInputError(f"artist {artist_id} has no contracts on file")
    docs = {row["id"]: parse_terms_doc(json.loads(row["terms"])) for row in contract_rows}
    bases = tuple(
        sorted(
            (docs[row["id"]] for row in contract_rows if row["kind"] == "base"),
            key=lambda d: (d.effective_from, d.contract_id),
        )
    )
    if not bases:
        raise CalcInputError(f"artist {artist_id} has no base contract")
    amendments_of: dict[int, list[TermsDoc]] = {}
    for row in contract_rows:
        if row["kind"] == "amendment" and row["supersedes_contract_id"] is not None:
            amendments_of.setdefault(row["supersedes_contract_id"], []).append(docs[row["id"]])

    fx_rows = await source.fetch(
        "SELECT period, currency, usd_rate FROM label.fx_rates ORDER BY period, currency"
    )
    fx: dict[str, dict[str, Decimal]] = {}
    for row in fx_rows:
        fx.setdefault(row["period"], {})[row["currency"]] = row["usd_rate"]

    isrc_rows = await source.fetch(
        """
        SELECT t.isrc, MIN(r.release_date) AS origin
        FROM label.tracks t
        JOIN label.release_tracks rt ON rt.track_id = t.id
        JOIN label.releases r ON r.id = rt.release_id
        WHERE t.primary_artist_id = $1
        GROUP BY t.isrc
        """,
        artist_id,
    )
    upc_rows = await source.fetch(
        """
        SELECT r.upc, r.release_date
        FROM label.releases r
        JOIN label.release_tracks rt ON rt.release_id = r.id
        JOIN label.tracks t ON t.id = rt.track_id
        GROUP BY r.upc, r.release_date
        HAVING COUNT(DISTINCT t.primary_artist_id) = 1
           AND MIN(t.primary_artist_id) = $1
        """,
        artist_id,
    )
    balance_rows = await source.fetch(
        "SELECT xcollat_group_id, opening_balance FROM label.recoup_accounts WHERE artist_id = $1",
        artist_id,
    )
    advance_rows = await source.fetch(
        "SELECT contract_id, amount, granted_at FROM label.advances WHERE artist_id = $1 "
        "ORDER BY id",
        artist_id,
    )
    expense_rows = await source.fetch(
        "SELECT amount, incurred_at FROM label.expenses "
        "WHERE artist_id = $1 AND recoupable ORDER BY id",
        artist_id,
    )
    return _ArtistWorld(
        artist_id=artist_id,
        bases=bases,
        amendments_of={k: tuple(v) for k, v in amendments_of.items()},
        fx=fx,
        periods=tuple(sorted(fx)),
        isrc_origin={r["isrc"]: r["origin"] for r in isrc_rows},
        upc_release={r["upc"]: r["release_date"] for r in upc_rows},
        opening_balances={r["xcollat_group_id"]: r["opening_balance"] for r in balance_rows},
        advances=tuple((r["contract_id"], r["amount"], r["granted_at"]) for r in advance_rows),
        expenses=tuple((r["amount"], r["incurred_at"]) for r in expense_rows),
    )


def _governing_terms(world: _ArtistWorld, base_id: int, period: str) -> Terms:
    base = next(b for b in world.bases if b.contract_id == base_id)
    return resolve_terms(base, world.amendments_of.get(base_id, ()), as_of=period_end_date(period))


async def _fetch_lines(
    source: asyncpg.Pool | asyncpg.Connection,
    world: _ArtistWorld,
    *,
    through_period: str,
    include_staged: bool,
) -> list[_SourceLine]:
    isrcs = sorted(world.isrc_origin)
    upcs = sorted(world.upc_release)
    rows = await source.fetch(
        """
        SELECT id, period, isrc, upc, store, territory, units, gross_amount, currency
        FROM label.statement_lines
        WHERE period <= $1 AND (isrc = ANY($2::text[])
                                OR (isrc = '' AND upc = ANY($3::text[])))
        ORDER BY id
        """,
        through_period,
        isrcs,
        upcs,
    )
    lines = [
        _SourceLine(
            id=r["id"],
            period=r["period"],
            isrc=r["isrc"],
            upc=r["upc"],
            store=r["store"],
            territory=r["territory"],
            units=r["units"],
            gross_amount=r["gross_amount"],
            currency=r["currency"],
            staged=False,
        )
        for r in rows
    ]
    if include_staged:
        staged = await source.fetch(
            """
            SELECT id, period, isrc, upc, store, territory, units, gross_amount, currency
            FROM staging.ingested_lines
            WHERE period <= $1 AND (isrc = ANY($2::text[])
                                    OR (isrc = '' AND upc = ANY($3::text[])))
            ORDER BY id
            """,
            through_period,
            isrcs,
            upcs,
        )
        lines.extend(
            _SourceLine(
                id=r["id"],
                period=r["period"],
                isrc=r["isrc"],
                upc=r["upc"],
                store=r["store"],
                territory=r["territory"],
                units=r["units"],
                gross_amount=r["gross_amount"],
                currency=r["currency"],
                staged=True,
            )
            for r in staged
        )
    return lines


def _charges_by_period(world: _ArtistWorld, periods: set[str]) -> dict[str, dict[str, Decimal]]:
    charges: dict[str, dict[str, Decimal]] = {}
    for contract_id, amount, granted_at in world.advances:
        period = granted_at.isoformat()[:7]
        if period not in periods:
            continue
        account = _governing_terms(world, contract_id, period).account
        bucket = charges.setdefault(period, {})
        bucket[account] = bucket.get(account, ZERO) + money6(amount)
    for amount, incurred_at in world.expenses:
        period = incurred_at.isoformat()[:7]
        if period not in periods:
            continue
        era = _era_for(world.bases, incurred_at)
        account = _governing_terms(world, era.contract_id, period).account
        bucket = charges.setdefault(period, {})
        bucket[account] = bucket.get(account, ZERO) + money6(amount)
    return charges


def _run_chain(
    world: _ArtistWorld,
    lines_by_period: dict[str, list[RevenueLine]],
    through: str,
) -> tuple[PeriodOutcome, str]:
    """Run the engine from the first FX period through ``through``; return the final
    period's outcome and the start period."""
    periods = [p for p in world.periods if p <= through]
    charges = _charges_by_period(world, set(periods))
    state = ArtistState.initial(world.opening_balances)
    outcome: PeriodOutcome | None = None
    for period in periods:
        end = period_end_date(period)
        base_ids = [b.contract_id for b in world.bases if b.effective_from <= end]
        terms_by_contract = {bid: _governing_terms(world, bid, period) for bid in base_ids}
        outcome = compute_artist_period(
            lines=lines_by_period.get(period, []),
            terms_by_contract=terms_by_contract,
            fx=world.fx[period],
            state=state,
            period_charges=charges.get(period, {}),
        )
        state = outcome.state
    assert outcome is not None  # periods is never empty (validated by callers)
    return outcome, periods[0]


def _attribute(
    world: _ArtistWorld, lines: list[_SourceLine]
) -> tuple[dict[str, list[RevenueLine]], int]:
    """Bucket source lines into engine lines per period. Returns (buckets, staged
    count). Raises on an unknown store — that's config drift, not agent error."""
    store_types = store_revenue_types()
    buckets: dict[str, list[RevenueLine]] = {}
    staged_used = 0
    for line in lines:
        revenue_type = store_types.get(line.store)
        if revenue_type is None:
            raise CalcInputError(
                f"line {line.id} names unknown store {line.store!r} — the store "
                f"reference (datagen/world.yaml) does not classify it"
            )
        origin = (
            world.isrc_origin.get(line.isrc) if line.isrc else world.upc_release.get(line.upc or "")
        )
        if origin is None:  # unmatched catalog — caller filtered, defensive here
            continue
        era = _era_for(world.bases, origin)
        buckets.setdefault(line.period, []).append(
            RevenueLine(
                contract_id=era.contract_id,
                revenue_type=revenue_type,
                territory=line.territory,
                amount=line.gross_amount,
                currency=line.currency,
            )
        )
        if line.staged:
            staged_used += 1
    return buckets, staged_used


async def compute_ledger_slice(
    source: asyncpg.Pool | asyncpg.Connection,
    *,
    artist_id: int,
    period: str,
    exclude_line_ids: tuple[int, ...] | list[int] = (),
    exclude_staged_line_ids: tuple[int, ...] | list[int] = (),
    include_staged: bool = False,
) -> LedgerSlice:
    world = await _load_artist_world(source, artist_id)
    if period not in world.periods:
        raise CalcInputError(
            f"period {period!r} has no FX row — computable periods: "
            f"{world.periods[0]}..{world.periods[-1]}"
        )
    raw_lines = await _fetch_lines(
        source, world, through_period=period, include_staged=include_staged
    )
    # Label and staged ids are separate sequences and can collide numerically —
    # exclusions are therefore matched per source, never by bare id across both.
    excluded_set = set(exclude_line_ids)
    excluded_staged_set = set(exclude_staged_line_ids)
    used: list[_SourceLine] = []
    excluded_found: list[int] = []
    excluded_staged_found: list[int] = []
    auto_excluded: list[int] = []
    for line in raw_lines:
        if line.staged and line.id in excluded_staged_set:
            excluded_staged_found.append(line.id)
            continue
        if not line.staged and line.id in excluded_set:
            excluded_found.append(line.id)
            continue
        if line.units <= 0 or line.gross_amount < 0:
            auto_excluded.append(line.id)
            continue
        used.append(line)

    buckets, staged_used = _attribute(world, used)
    outcome, start_period = _run_chain(world, buckets, period)

    # The rollup reports the *target period* (the outcome), not the whole chain.
    by_type: dict[str, tuple[int, Decimal]] = {}
    for line_royalty in outcome.lines:
        n, total = by_type.get(line_royalty.revenue_type, (0, ZERO))
        by_type[line_royalty.revenue_type] = (n + 1, total + line_royalty.royalty)

    return LedgerSlice(
        artist_id=artist_id,
        period=period,
        start_period=start_period,
        gross=outcome.gross,
        recouped=outcome.recouped,
        mg_topup=outcome.mg_topup,
        net_payable=outcome.net_payable,
        balance_after=outcome.balance_after,
        outcome=outcome,
        by_revenue_type=by_type,
        contracts_used=tuple(sorted({rl.contract_id for rl in outcome.lines})),
        n_lines_used=len(used),
        n_staged_used=staged_used,
        excluded_line_ids=tuple(sorted(excluded_found)),
        excluded_staged_line_ids=tuple(sorted(excluded_staged_found)),
        auto_excluded_line_ids=tuple(sorted(auto_excluded)),
    )


async def compute_spot_quote(
    source: asyncpg.Pool | asyncpg.Connection,
    *,
    artist_id: int,
    rows: list[tuple[str, str, Decimal, str]],  # (revenue_type, territory, amount, currency)
    as_of: date | None,
) -> SpotQuote:
    world = await _load_artist_world(source, artist_id)
    if as_of is None:
        as_of = period_end_date(world.periods[-1])
    as_of_period = as_of.isoformat()[:7]
    fx_period = as_of_period if as_of_period in world.fx else world.periods[-1]

    era = _era_for(world.bases, as_of)
    terms = resolve_terms(era, world.amendments_of.get(era.contract_id, ()), as_of=as_of)

    # Escalator state = cumulative gross at the start of the period containing as_of:
    # run the real chain through the *prior* period (exact, matches the engine).
    prior = [p for p in world.periods if p < as_of_period]
    cumulative = ZERO
    if prior:
        raw_lines = await _fetch_lines(
            source, world, through_period=prior[-1], include_staged=False
        )
        valid = [ln for ln in raw_lines if ln.units > 0 and ln.gross_amount >= 0]
        buckets, _ = _attribute(world, valid)
        outcome, _ = _run_chain(world, buckets, prior[-1])
        cumulative = outcome.state.cumulative_gross_usd.get(era.contract_id, ZERO)

    spot_lines: list[SpotLine] = []
    total = ZERO
    for revenue_type, territory, amount, currency in rows:
        usd = to_usd(money6(amount), currency, world.fx[fx_period])
        rate = effective_rate(terms, revenue_type, territory, cumulative)
        royalty = money6(usd * rate)
        total += royalty
        spot_lines.append(
            SpotLine(
                revenue_type=revenue_type,
                territory=territory,
                amount=money6(amount),
                currency=currency,
                usd_amount=usd,
                rate=rate,
                royalty=royalty,
            )
        )
    return SpotQuote(
        artist_id=artist_id,
        contract_id=era.contract_id,
        as_of=as_of,
        fx_period=fx_period,
        cumulative_gross_usd=cumulative,
        active_bump=escalator_bump(terms, cumulative),
        lines=tuple(spot_lines),
        total_royalty=money6(total),
    )
