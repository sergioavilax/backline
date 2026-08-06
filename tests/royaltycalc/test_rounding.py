"""The rounding policy — the one place money precision is decided (BUILD_PLAN §0, invariant 1).

Line-level amounts keep 6 decimal places; artist-facing totals round half-even to cents
at final aggregation only. Both quantizers live in ``backline.royaltycalc.rounding`` and
nowhere else.
"""

from decimal import Decimal

import pytest

from backline.royaltycalc.rounding import CENT, SIX, ZERO, money6, to_cents


class TestMoney6:
    def test_quantizes_to_six_places(self) -> None:
        assert money6(Decimal("1.23456789")) == Decimal("1.234568")

    def test_half_even_ties(self) -> None:
        # Ties go to the even neighbor, not away from zero.
        assert money6(Decimal("0.0000005")) == Decimal("0.000000")
        assert money6(Decimal("0.0000015")) == Decimal("0.000002")
        assert money6(Decimal("0.0000025")) == Decimal("0.000002")

    def test_accepts_int_and_str(self) -> None:
        assert money6(3) == Decimal("3.000000")
        assert money6("2.5") == Decimal("2.500000")

    def test_rejects_float(self) -> None:
        with pytest.raises(TypeError, match="float"):
            money6(1.23)  # type: ignore[arg-type]

    def test_rejects_other_types(self) -> None:
        with pytest.raises(TypeError):
            money6(None)  # type: ignore[arg-type]

    def test_idempotent(self) -> None:
        once = money6(Decimal("9.87654321"))
        assert money6(once) == once

    def test_negative_half_even(self) -> None:
        # -1.2345675: the kept digit (7) is odd, so the tie rounds to -1.234568.
        assert money6(Decimal("-1.2345675")) == Decimal("-1.234568")

    def test_exponent_is_exactly_minus_six(self) -> None:
        assert money6(Decimal("1")).as_tuple().exponent == -6
        assert money6(ZERO).as_tuple().exponent == -6


class TestToCents:
    def test_half_even_ties(self) -> None:
        assert to_cents(Decimal("1.005")) == Decimal("1.00")
        assert to_cents(Decimal("1.015")) == Decimal("1.02")
        assert to_cents(Decimal("1.025")) == Decimal("1.02")

    def test_plain_rounding(self) -> None:
        assert to_cents(Decimal("12.345678")) == Decimal("12.35")
        assert to_cents(Decimal("12.344999")) == Decimal("12.34")

    def test_rejects_float(self) -> None:
        with pytest.raises(TypeError, match="float"):
            to_cents(1.005)  # type: ignore[arg-type]

    def test_exponent_is_exactly_minus_two(self) -> None:
        assert to_cents(Decimal("7")).as_tuple().exponent == -2

    def test_negative(self) -> None:
        assert to_cents(Decimal("-0.005")) == Decimal("-0.00")
        assert to_cents(Decimal("-0.015")) == Decimal("-0.02")


def test_quantum_constants() -> None:
    assert Decimal("0.000001") == SIX
    assert Decimal("0.01") == CENT
    assert Decimal("0") == ZERO
