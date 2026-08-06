"""Rate resolution: rate card lookup, territory fallback, carve-outs, escalators."""

from decimal import Decimal

import pytest

from backline.royaltycalc.rates import base_rate, effective_rate, escalator_bump
from backline.royaltycalc.terms import Escalator, RateCardEntry, Terms

D = Decimal


def mk_terms(
    *,
    rate_card: tuple[RateCardEntry, ...] | None = None,
    escalators: tuple[Escalator, ...] = (),
    excluded: frozenset[str] = frozenset(),
) -> Terms:
    if rate_card is None:
        rate_card = (
            RateCardEntry("streaming", "WW", D("0.30")),
            RateCardEntry("physical", "GB", D("0.15")),
            RateCardEntry("physical", "WW", D("0.10")),
        )
    return Terms(
        contract_id=1,
        artist_id=1,
        account="A",
        rate_card=rate_card,
        escalators=escalators,
        excluded_territories=excluded,
        recoupable_classes=frozenset({"recording"}),
        minimum_guarantee_per_period=None,
    )


class TestBaseRate:
    def test_worldwide_fallback(self) -> None:
        assert base_rate(mk_terms(), "streaming", "US") == D("0.30")

    def test_specific_territory_beats_worldwide(self) -> None:
        terms = mk_terms()
        assert base_rate(terms, "physical", "GB") == D("0.15")
        assert base_rate(terms, "physical", "DE") == D("0.10")

    def test_uncovered_revenue_type_is_zero(self) -> None:
        assert base_rate(mk_terms(), "sync", "US") == D("0")

    def test_excluded_territory_is_zero(self) -> None:
        terms = mk_terms(excluded=frozenset({"JP"}))
        assert base_rate(terms, "streaming", "JP") == D("0")
        assert base_rate(terms, "streaming", "US") == D("0.30")

    def test_unknown_revenue_type_raises(self) -> None:
        with pytest.raises(ValueError, match="merch"):
            base_rate(mk_terms(), "merch", "US")


class TestEscalators:
    two_tiers = (
        Escalator(threshold_usd=D("250000"), bump=D("0.02")),
        Escalator(threshold_usd=D("500000"), bump=D("0.03")),
    )

    def test_no_escalators_no_bump(self) -> None:
        assert escalator_bump(mk_terms(), D("999999999")) == D("0")

    def test_below_first_threshold(self) -> None:
        terms = mk_terms(escalators=self.two_tiers)
        assert escalator_bump(terms, D("249999.999999")) == D("0")

    def test_at_threshold_bumps(self) -> None:
        terms = mk_terms(escalators=self.two_tiers)
        assert escalator_bump(terms, D("250000")) == D("0.02")

    def test_highest_crossed_tier_wins_not_additive(self) -> None:
        terms = mk_terms(escalators=self.two_tiers)
        assert escalator_bump(terms, D("600000")) == D("0.03")

    def test_effective_rate_applies_bump(self) -> None:
        terms = mk_terms(escalators=self.two_tiers)
        assert effective_rate(terms, "streaming", "US", D("0")) == D("0.30")
        assert effective_rate(terms, "streaming", "US", D("250000")) == D("0.32")

    def test_bump_never_applies_to_zero_base(self) -> None:
        terms = mk_terms(escalators=self.two_tiers, excluded=frozenset({"JP"}))
        assert effective_rate(terms, "streaming", "JP", D("999999")) == D("0")
        assert effective_rate(terms, "sync", "US", D("999999")) == D("0")

    def test_rate_capped_at_one(self) -> None:
        terms = mk_terms(
            rate_card=(RateCardEntry("sync", "WW", D("0.99")),),
            escalators=(Escalator(threshold_usd=D("10"), bump=D("0.05")),),
        )
        assert effective_rate(terms, "sync", "US", D("100")) == D("1")
