"""The artist-period royalty engine — the one implementation of royalty math (D-001).

Callers (the datagen truth engine today; the runtime ``calc_royalties`` tool in Phase 3)
attribute revenue lines to contracts, resolve governing terms as of the period, and hand
everything here. The engine rates each line at 6dp, pools earnings per recoupment account
(cross-collateralization is just two contracts sharing an account), runs the waterfall,
applies any minimum guarantee, and rounds the artist-facing net to cents exactly once.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal

from backline.royaltycalc.fx import to_usd
from backline.royaltycalc.rates import effective_rate
from backline.royaltycalc.recoupment import apply_minimum_guarantee, recoup
from backline.royaltycalc.rounding import ZERO, money6, to_cents
from backline.royaltycalc.terms import Terms

ZERO6 = money6(0)


@dataclass(frozen=True)
class RevenueLine:
    """One attributable revenue line: label net receipts in the statement currency."""

    contract_id: int
    revenue_type: str
    territory: str
    amount: Decimal
    currency: str


@dataclass(frozen=True)
class LineRoyalty:
    contract_id: int
    revenue_type: str
    territory: str
    usd_amount: Decimal
    rate: Decimal
    royalty: Decimal


@dataclass(frozen=True)
class AccountOutcome:
    account: str
    earnings: Decimal
    charges: Decimal
    balance_open: Decimal
    recouped: Decimal
    mg_topup: Decimal
    payable_raw: Decimal
    balance_after: Decimal


@dataclass(frozen=True)
class ArtistState:
    """Carry-forward state between periods: escalator cumulative and account balances."""

    cumulative_gross_usd: Mapping[int, Decimal]  # by contract id, excludes carve-outs
    balances: Mapping[str, Decimal]  # by recoupment account, always >= 0

    @classmethod
    def initial(cls, opening_balances: Mapping[str, Decimal]) -> ArtistState:
        return cls(
            cumulative_gross_usd={},
            balances={acct: money6(bal) for acct, bal in opening_balances.items()},
        )


@dataclass(frozen=True)
class PeriodOutcome:
    gross: Decimal  # sum of line royalties, 6dp
    recouped: Decimal  # 6dp
    mg_topup: Decimal  # 6dp
    payable_raw: Decimal  # gross - recouped + mg_topup, 6dp (pre-rounding)
    net_payable: Decimal  # to_cents(payable_raw) — the artist-facing figure
    balance_after: Decimal  # sum across accounts, 6dp
    lines: tuple[LineRoyalty, ...]
    accounts: tuple[AccountOutcome, ...]
    state: ArtistState  # next period's carry-forward


def compute_artist_period(
    *,
    lines: Sequence[RevenueLine],
    terms_by_contract: Mapping[int, Terms],
    fx: Mapping[str, Decimal],
    state: ArtistState,
    period_charges: Mapping[str, Decimal],
) -> PeriodOutcome:
    """Compute one artist-period. Pure: returns a fresh outcome + next state.

    ``period_charges`` are the period's new advances plus recoupable expenses, already
    summed per account by the caller; they land on the balance *before* recoupment.
    Escalators read cumulative gross at period start (``state``), so a threshold crossed
    during a period affects following periods only.
    """
    line_outcomes: list[LineRoyalty] = []
    earnings_by_account: dict[str, Decimal] = {}
    gross_usd_add: dict[int, Decimal] = {}

    for line in lines:
        terms = terms_by_contract.get(line.contract_id)
        if terms is None:
            raise ValueError(f"no governing terms for contract {line.contract_id}")
        amount = money6(line.amount)
        if amount < 0:
            raise ValueError(f"negative line amount {amount} (contract {line.contract_id})")
        usd = to_usd(amount, line.currency, fx)
        cumulative_before = state.cumulative_gross_usd.get(line.contract_id, ZERO)
        rate = effective_rate(terms, line.revenue_type, line.territory, cumulative_before)
        royalty = money6(usd * rate)
        line_outcomes.append(
            LineRoyalty(
                contract_id=line.contract_id,
                revenue_type=line.revenue_type,
                territory=line.territory,
                usd_amount=usd,
                rate=rate,
                royalty=royalty,
            )
        )
        acct = terms.account
        earnings_by_account[acct] = earnings_by_account.get(acct, ZERO6) + royalty
        if line.territory not in terms.excluded_territories:
            gross_usd_add[line.contract_id] = gross_usd_add.get(line.contract_id, ZERO6) + usd

    mg_by_account: dict[str, Decimal] = {}
    for terms in terms_by_contract.values():
        if terms.minimum_guarantee_per_period is not None:
            mg = money6(terms.minimum_guarantee_per_period)
            prior = mg_by_account.get(terms.account)
            mg_by_account[terms.account] = mg if prior is None else max(prior, mg)

    accounts = sorted(
        set(earnings_by_account)
        | set(period_charges)
        | set(state.balances)
        | set(mg_by_account)
        | {t.account for t in terms_by_contract.values()}
    )

    account_outcomes: list[AccountOutcome] = []
    new_balances: dict[str, Decimal] = {}
    for acct in accounts:
        earnings = earnings_by_account.get(acct, ZERO6)
        charges = money6(period_charges.get(acct, ZERO))
        if charges < 0:
            raise ValueError(f"negative charge for account {acct!r}")
        balance_open = money6(state.balances.get(acct, ZERO)) + charges
        result = recoup(earnings, balance_open)
        payable_raw, topup = apply_minimum_guarantee(result.payable_raw, mg_by_account.get(acct))
        balance_after = result.balance_after + topup
        account_outcomes.append(
            AccountOutcome(
                account=acct,
                earnings=earnings,
                charges=charges,
                balance_open=balance_open,
                recouped=result.recouped,
                mg_topup=topup,
                payable_raw=payable_raw,
                balance_after=balance_after,
            )
        )
        new_balances[acct] = balance_after

    gross = sum((a.earnings for a in account_outcomes), ZERO6)
    recouped = sum((a.recouped for a in account_outcomes), ZERO6)
    mg_topup = sum((a.mg_topup for a in account_outcomes), ZERO6)
    payable_raw = sum((a.payable_raw for a in account_outcomes), ZERO6)
    balance_after_total = sum((a.balance_after for a in account_outcomes), ZERO6)

    new_cumulative = dict(state.cumulative_gross_usd)
    for contract_id, usd_add in gross_usd_add.items():
        new_cumulative[contract_id] = new_cumulative.get(contract_id, ZERO6) + usd_add

    return PeriodOutcome(
        gross=gross,
        recouped=recouped,
        mg_topup=mg_topup,
        payable_raw=payable_raw,
        net_payable=to_cents(payable_raw),
        balance_after=balance_after_total,
        lines=tuple(line_outcomes),
        accounts=tuple(account_outcomes),
        state=ArtistState(cumulative_gross_usd=new_cumulative, balances=new_balances),
    )
