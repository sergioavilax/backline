"""FX normalization: everything converts to USD through the period's fixed rate table."""

from decimal import Decimal

import pytest

from backline.royaltycalc.fx import to_usd

FX = {
    "USD": Decimal("1"),
    "EUR": Decimal("1.0850"),
    "GBP": Decimal("1.2700"),
    "JPY": Decimal("0.006500"),
}


def test_usd_identity() -> None:
    assert to_usd(Decimal("12.345678"), "USD", FX) == Decimal("12.345678")


def test_eur_conversion() -> None:
    assert to_usd(Decimal("100.000000"), "EUR", FX) == Decimal("108.500000")


def test_jpy_micro_amounts_keep_six_places() -> None:
    assert to_usd(Decimal("1234.000000"), "JPY", FX) == Decimal("8.021000")
    assert to_usd(Decimal("1.000000"), "JPY", FX) == Decimal("0.006500")


def test_result_is_quantized_to_six_places() -> None:
    out = to_usd(Decimal("0.333333"), "GBP", FX)
    assert out.as_tuple().exponent == -6
    assert out == Decimal("0.423333")  # 0.333333 * 1.27 = 0.42333291 -> 6dp


def test_unknown_currency_raises() -> None:
    with pytest.raises(ValueError, match="CHF"):
        to_usd(Decimal("1"), "CHF", FX)


def test_rejects_float_amount() -> None:
    with pytest.raises(TypeError, match="float"):
        to_usd(1.5, "USD", FX)  # type: ignore[arg-type]
