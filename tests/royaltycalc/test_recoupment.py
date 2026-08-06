"""Recoupment waterfall primitives: recoup against a balance, minimum-guarantee top-up."""

from decimal import Decimal

import pytest

from backline.royaltycalc.recoupment import apply_minimum_guarantee, recoup

D = Decimal


class TestRecoup:
    def test_partial_recoup(self) -> None:
        r = recoup(earnings=D("100"), balance_open=D("40"))
        assert r.recouped == D("40")
        assert r.payable_raw == D("60")
        assert r.balance_after == D("0")

    def test_full_recoup_leaves_balance(self) -> None:
        r = recoup(earnings=D("100"), balance_open=D("250"))
        assert r.recouped == D("100")
        assert r.payable_raw == D("0")
        assert r.balance_after == D("150")

    def test_zero_earnings(self) -> None:
        r = recoup(earnings=D("0"), balance_open=D("50"))
        assert r.recouped == D("0")
        assert r.payable_raw == D("0")
        assert r.balance_after == D("50")

    def test_zero_balance_passes_through(self) -> None:
        r = recoup(earnings=D("12.345678"), balance_open=D("0"))
        assert r.recouped == D("0")
        assert r.payable_raw == D("12.345678")
        assert r.balance_after == D("0")

    def test_negative_earnings_rejected(self) -> None:
        with pytest.raises(ValueError, match="earnings"):
            recoup(earnings=D("-1"), balance_open=D("0"))

    def test_negative_balance_rejected(self) -> None:
        with pytest.raises(ValueError, match="balance"):
            recoup(earnings=D("1"), balance_open=D("-0.01"))


class TestMinimumGuarantee:
    def test_none_is_passthrough(self) -> None:
        assert apply_minimum_guarantee(D("60"), None) == (D("60"), D("0"))

    def test_tops_up_below_guarantee(self) -> None:
        payable, topup = apply_minimum_guarantee(D("60"), D("100"))
        assert payable == D("100")
        assert topup == D("40")

    def test_no_topup_above_guarantee(self) -> None:
        assert apply_minimum_guarantee(D("150"), D("100")) == (D("150"), D("0"))

    def test_zero_payable_gets_full_guarantee(self) -> None:
        payable, topup = apply_minimum_guarantee(D("0"), D("100"))
        assert payable == D("100")
        assert topup == D("100")

    def test_negative_guarantee_rejected(self) -> None:
        with pytest.raises(ValueError, match="guarantee"):
            apply_minimum_guarantee(D("10"), D("-5"))
