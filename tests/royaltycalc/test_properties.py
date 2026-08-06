"""Property-based invariants (Hypothesis) for the royalty engine (BUILD_PLAN Phase 1).

The two invariants named in the plan — allocations sum to gross minus deductions, and
balances never double-recoup — plus quantization laws. Strategies are constrained to sane
Decimal scales (6dp money, 3dp rates) so shrinking stays fast (BUILD_PLAN §9).
"""

from decimal import Decimal

from hypothesis import given
from hypothesis import strategies as st

from backline.royaltycalc.engine import ArtistState, RevenueLine, compute_artist_period
from backline.royaltycalc.rates import effective_rate
from backline.royaltycalc.rounding import money6, to_cents
from backline.royaltycalc.terms import Escalator, RateCardEntry, Terms

D = Decimal
FX = {"USD": D("1"), "EUR": D("1.0850"), "GBP": D("1.2700"), "JPY": D("0.006500")}

# 6dp non-negative money up to 10^6 USD; 3dp rates in [0, 1].
money = st.integers(min_value=0, max_value=10**12).map(lambda n: D(n).scaleb(-6))
rate = st.integers(min_value=0, max_value=1000).map(lambda n: D(n).scaleb(-3))
currency = st.sampled_from(sorted(FX))


def terms_for(r: Decimal, mg: Decimal | None = None) -> Terms:
    return Terms(
        contract_id=1,
        artist_id=1,
        account="A",
        rate_card=(RateCardEntry("streaming", "WW", r),),
        escalators=(),
        excluded_territories=frozenset(),
        recoupable_classes=frozenset(),
        minimum_guarantee_per_period=mg,
    )


@given(
    amounts=st.lists(st.tuples(money, currency), max_size=12),
    r=rate,
    opening=money,
    charge=money,
    mg=st.none() | money,
)
def test_period_identities(
    amounts: list[tuple[Decimal, str]],
    r: Decimal,
    opening: Decimal,
    charge: Decimal,
    mg: Decimal | None,
) -> None:
    lines = [RevenueLine(1, "streaming", "US", amt, ccy) for amt, ccy in amounts]
    out = compute_artist_period(
        lines=lines,
        terms_by_contract={1: terms_for(r, mg)},
        fx=FX,
        state=ArtistState.initial({"A": opening}),
        period_charges={"A": charge},
    )
    # Allocations sum to gross: the period gross is exactly the sum of line royalties.
    assert out.gross == sum((line.royalty for line in out.lines), D("0.000000"))
    # Payable is gross minus deductions (recoupment) plus any MG top-up — exactly.
    assert out.payable_raw == out.gross - out.recouped + out.mg_topup
    # The artist-facing figure is the cents-rounding of payable_raw, nothing else.
    assert out.net_payable == to_cents(out.payable_raw)
    assert abs(out.net_payable - out.payable_raw) <= D("0.005")
    # Balance bookkeeping: open + charges - recouped + topup, never negative.
    assert out.balance_after == opening + charge - out.recouped + out.mg_topup
    assert out.balance_after >= 0
    # Never recoup more than earned, never more than owed.
    assert out.recouped <= out.gross
    assert out.recouped <= opening + charge
    # Everything monetary is quantized to 6dp (net to cents).
    for value in (out.gross, out.recouped, out.payable_raw, out.balance_after):
        assert value.as_tuple().exponent == -6
    assert out.net_payable.as_tuple().exponent == -2


@given(
    periods=st.lists(st.tuples(money, money), min_size=1, max_size=8),
    r=rate,
    opening=money,
)
def test_balances_never_double_recoup_across_periods(
    periods: list[tuple[Decimal, Decimal]],
    r: Decimal,
    opening: Decimal,
) -> None:
    terms = {1: terms_for(r)}
    state = ArtistState.initial({"A": opening})
    total_recouped = D("0")
    total_charges = D("0")
    for amount, charge in periods:
        out = compute_artist_period(
            lines=[RevenueLine(1, "streaming", "US", amount, "USD")],
            terms_by_contract=terms,
            fx=FX,
            state=state,
            period_charges={"A": charge},
        )
        state = out.state
        total_recouped += out.recouped
        total_charges += charge
        assert out.balance_after >= 0
    # Across the whole history: recouped never exceeds what was actually charged.
    assert total_recouped <= opening + total_charges
    # And the final balance closes the books exactly.
    assert state.balances["A"] == money6(opening + total_charges - total_recouped)


@given(
    periods=st.lists(st.tuples(money, money), min_size=1, max_size=6),
    r=rate,
    opening=money,
)
def test_engine_is_deterministic(
    periods: list[tuple[Decimal, Decimal]],
    r: Decimal,
    opening: Decimal,
) -> None:
    def run() -> list[tuple[Decimal, Decimal, Decimal, Decimal]]:
        state = ArtistState.initial({"A": opening})
        rows = []
        for amount, charge in periods:
            out = compute_artist_period(
                lines=[RevenueLine(1, "streaming", "US", amount, "USD")],
                terms_by_contract={1: terms_for(r)},
                fx=FX,
                state=state,
                period_charges={"A": charge},
            )
            state = out.state
            rows.append((out.gross, out.recouped, out.net_payable, out.balance_after))
        return rows

    assert run() == run()


@given(cumulative_lo=money, cumulative_hi=money)
def test_escalators_are_monotone_in_cumulative_revenue(
    cumulative_lo: Decimal, cumulative_hi: Decimal
) -> None:
    lo, hi = sorted((cumulative_lo, cumulative_hi))
    terms = Terms(
        contract_id=1,
        artist_id=1,
        account="A",
        rate_card=(RateCardEntry("streaming", "WW", D("0.25")),),
        escalators=(
            Escalator(threshold_usd=D("100"), bump=D("0.02")),
            Escalator(threshold_usd=D("100000"), bump=D("0.04")),
        ),
        excluded_territories=frozenset(),
        recoupable_classes=frozenset(),
        minimum_guarantee_per_period=None,
    )
    assert effective_rate(terms, "streaming", "US", lo) <= effective_rate(
        terms, "streaming", "US", hi
    )


@given(value=st.integers(min_value=-(10**13), max_value=10**13).map(lambda n: D(n).scaleb(-7)))
def test_rounding_laws(value: Decimal) -> None:
    assert money6(money6(value)) == money6(value)
    assert abs(to_cents(value) - value) <= D("0.005")
    assert money6(value).as_tuple().exponent == -6
    assert to_cents(value).as_tuple().exponent == -2
