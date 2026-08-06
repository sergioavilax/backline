"""Engine scenarios: one artist-period at a time, exactly as the truth engine and the
runtime calculator tool will call it (same code — D-001)."""

from decimal import Decimal

import pytest

from backline.royaltycalc.engine import ArtistState, RevenueLine, compute_artist_period
from backline.royaltycalc.terms import Escalator, RateCardEntry, Terms

D = Decimal

FX = {
    "USD": D("1"),
    "EUR": D("1.0850"),
    "GBP": D("1.2700"),
    "JPY": D("0.006500"),
}


def mk_terms(
    contract_id: int = 1,
    account: str = "A",
    *,
    rate_card: tuple[RateCardEntry, ...] = (RateCardEntry("streaming", "WW", D("0.30")),),
    escalators: tuple[Escalator, ...] = (),
    excluded: frozenset[str] = frozenset(),
    mg: Decimal | None = None,
) -> Terms:
    return Terms(
        contract_id=contract_id,
        artist_id=1,
        account=account,
        rate_card=rate_card,
        escalators=escalators,
        excluded_territories=excluded,
        recoupable_classes=frozenset({"recording"}),
        minimum_guarantee_per_period=mg,
    )


def usd_line(amount: str, contract_id: int = 1, territory: str = "US") -> RevenueLine:
    return RevenueLine(
        contract_id=contract_id,
        revenue_type="streaming",
        territory=territory,
        amount=D(amount),
        currency="USD",
    )


class TestSingleContract:
    def test_simple_unrecouped_artist(self) -> None:
        out = compute_artist_period(
            lines=[usd_line("100.000000")],
            terms_by_contract={1: mk_terms()},
            fx=FX,
            state=ArtistState.initial({}),
            period_charges={},
        )
        assert out.gross == D("30.000000")
        assert out.recouped == D("0.000000")
        assert out.net_payable == D("30.00")
        assert out.balance_after == D("0.000000")
        assert out.state.cumulative_gross_usd[1] == D("100.000000")

    def test_multi_currency_line_level_precision(self) -> None:
        out = compute_artist_period(
            lines=[
                RevenueLine(1, "streaming", "DE", D("100.000000"), "EUR"),
                RevenueLine(1, "streaming", "JP", D("1234.000000"), "JPY"),
            ],
            terms_by_contract={1: mk_terms()},
            fx=FX,
            state=ArtistState.initial({}),
            period_charges={},
        )
        assert [line.usd_amount for line in out.lines] == [D("108.500000"), D("8.021000")]
        assert [line.royalty for line in out.lines] == [D("32.550000"), D("2.406300")]
        assert out.gross == D("34.956300")
        assert out.net_payable == D("34.96")
        assert out.payable_raw == D("34.956300")

    def test_charges_land_before_recoupment(self) -> None:
        out = compute_artist_period(
            lines=[usd_line("40.000000")],
            terms_by_contract={
                1: mk_terms(rate_card=(RateCardEntry("streaming", "WW", D("0.25")),))
            },
            fx=FX,
            state=ArtistState.initial({"A": D("10")}),
            period_charges={"A": D("5")},
        )
        assert out.gross == D("10.000000")
        assert out.recouped == D("10.000000")
        assert out.net_payable == D("0.00")
        assert out.balance_after == D("5.000000")

    def test_cents_rounding_half_even_at_final_aggregation(self) -> None:
        out = compute_artist_period(
            lines=[usd_line("0.250000")],
            terms_by_contract={
                1: mk_terms(rate_card=(RateCardEntry("streaming", "WW", D("0.10")),))
            },
            fx=FX,
            state=ArtistState.initial({}),
            period_charges={},
        )
        assert out.payable_raw == D("0.025000")
        assert out.net_payable == D("0.02")

    def test_escalator_uses_cumulative_at_period_start(self) -> None:
        terms = mk_terms(
            rate_card=(RateCardEntry("streaming", "WW", D("0.20")),),
            escalators=(Escalator(threshold_usd=D("100"), bump=D("0.10")),),
        )
        p1 = compute_artist_period(
            lines=[usd_line("100.000000")],
            terms_by_contract={1: terms},
            fx=FX,
            state=ArtistState.initial({}),
            period_charges={},
        )
        # The threshold is crossed *by* period 1, so period 1 itself stays at the base rate.
        assert p1.gross == D("20.000000")

        p2 = compute_artist_period(
            lines=[usd_line("50.000000")],
            terms_by_contract={1: terms},
            fx=FX,
            state=p1.state,
            period_charges={},
        )
        assert p2.lines[0].rate == D("0.30")
        assert p2.gross == D("15.000000")

    def test_excluded_territory_earns_nothing_and_skips_cumulative(self) -> None:
        terms = mk_terms(excluded=frozenset({"JP"}))
        out = compute_artist_period(
            lines=[usd_line("100.000000", territory="JP"), usd_line("50.000000")],
            terms_by_contract={1: terms},
            fx=FX,
            state=ArtistState.initial({}),
            period_charges={},
        )
        assert out.lines[0].royalty == D("0.000000")
        assert out.lines[1].royalty == D("15.000000")
        assert out.state.cumulative_gross_usd[1] == D("50.000000")


class TestAccounts:
    def test_cross_collateralized_contracts_pool_one_balance(self) -> None:
        terms = {
            1: mk_terms(1, "X"),
            2: mk_terms(2, "X", rate_card=(RateCardEntry("streaming", "WW", D("0.20")),)),
        }
        out = compute_artist_period(
            lines=[usd_line("100.000000", contract_id=1), usd_line("100.000000", contract_id=2)],
            terms_by_contract=terms,
            fx=FX,
            state=ArtistState.initial({"X": D("45")}),
            period_charges={},
        )
        assert out.gross == D("50.000000")
        assert out.recouped == D("45.000000")
        assert out.net_payable == D("5.00")
        assert out.balance_after == D("0.000000")
        assert out.state.cumulative_gross_usd == {1: D("100.000000"), 2: D("100.000000")}

    def test_independent_accounts_do_not_pool(self) -> None:
        terms = {
            1: mk_terms(1, "A"),
            2: mk_terms(2, "B", rate_card=(RateCardEntry("streaming", "WW", D("0.20")),)),
        }
        out = compute_artist_period(
            lines=[usd_line("100.000000", contract_id=1), usd_line("100.000000", contract_id=2)],
            terms_by_contract=terms,
            fx=FX,
            state=ArtistState.initial({"A": D("100")}),
            period_charges={},
        )
        # Contract 1's 30 all goes to its own balance; contract 2's 20 is untouched.
        assert out.recouped == D("30.000000")
        assert out.net_payable == D("20.00")
        assert out.balance_after == D("70.000000")

    def test_unreferenced_account_balances_carry_through(self) -> None:
        out = compute_artist_period(
            lines=[usd_line("10.000000")],
            terms_by_contract={1: mk_terms()},
            fx=FX,
            state=ArtistState.initial({"OLD": D("7")}),
            period_charges={},
        )
        assert out.state.balances["OLD"] == D("7.000000")
        assert out.balance_after == D("7.000000")

    def test_charges_to_account_without_lines_accrue(self) -> None:
        out = compute_artist_period(
            lines=[],
            terms_by_contract={1: mk_terms()},
            fx=FX,
            state=ArtistState.initial({}),
            period_charges={"A": D("250")},
        )
        assert out.gross == D("0.000000")
        assert out.net_payable == D("0.00")
        assert out.state.balances["A"] == D("250.000000")


class TestMinimumGuarantee:
    def test_topup_becomes_recoupable_balance(self) -> None:
        terms = {1: mk_terms(mg=D("50"))}
        p1 = compute_artist_period(
            lines=[usd_line("100.000000")],
            terms_by_contract=terms,
            fx=FX,
            state=ArtistState.initial({}),
            period_charges={},
        )
        assert p1.gross == D("30.000000")
        assert p1.net_payable == D("50.00")
        assert p1.mg_topup == D("20.000000")
        assert p1.state.balances["A"] == D("20.000000")

        p2 = compute_artist_period(
            lines=[usd_line("100.000000")],
            terms_by_contract=terms,
            fx=FX,
            state=p1.state,
            period_charges={},
        )
        # Earnings 30 recoup the 20 top-up first, then MG lifts the 10 remainder to 50.
        assert p2.recouped == D("20.000000")
        assert p2.net_payable == D("50.00")
        assert p2.mg_topup == D("40.000000")
        assert p2.state.balances["A"] == D("40.000000")

    def test_quiet_period_still_pays_guarantee(self) -> None:
        out = compute_artist_period(
            lines=[],
            terms_by_contract={1: mk_terms(mg=D("25"))},
            fx=FX,
            state=ArtistState.initial({}),
            period_charges={},
        )
        assert out.net_payable == D("25.00")
        assert out.mg_topup == D("25.000000")


class TestValidation:
    def test_missing_terms_for_contract_raises(self) -> None:
        with pytest.raises(ValueError, match="contract 9"):
            compute_artist_period(
                lines=[usd_line("1.000000", contract_id=9)],
                terms_by_contract={1: mk_terms()},
                fx=FX,
                state=ArtistState.initial({}),
                period_charges={},
            )

    def test_negative_line_amount_raises(self) -> None:
        with pytest.raises(ValueError, match="negative"):
            compute_artist_period(
                lines=[usd_line("-1.000000")],
                terms_by_contract={1: mk_terms()},
                fx=FX,
                state=ArtistState.initial({}),
                period_charges={},
            )

    def test_negative_charge_raises(self) -> None:
        with pytest.raises(ValueError, match="charge"):
            compute_artist_period(
                lines=[],
                terms_by_contract={1: mk_terms()},
                fx=FX,
                state=ArtistState.initial({}),
                period_charges={"A": D("-5")},
            )

    def test_unknown_currency_propagates(self) -> None:
        with pytest.raises(ValueError, match="CHF"):
            compute_artist_period(
                lines=[RevenueLine(1, "streaming", "US", D("1"), "CHF")],
                terms_by_contract={1: mk_terms()},
                fx=FX,
                state=ArtistState.initial({}),
                period_charges={},
            )

    def test_no_activity_is_a_clean_zero(self) -> None:
        state = ArtistState.initial({"A": D("10")})
        out = compute_artist_period(
            lines=[],
            terms_by_contract={1: mk_terms()},
            fx=FX,
            state=state,
            period_charges={},
        )
        assert out.gross == D("0.000000")
        assert out.recouped == D("0.000000")
        assert out.net_payable == D("0.00")
        assert out.balance_after == D("10.000000")
