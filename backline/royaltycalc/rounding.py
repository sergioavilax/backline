"""The single rounding policy for money (BUILD_PLAN §0, invariant 1).

Line-level amounts keep 6 decimal places (streaming micro-payments); artist-facing totals
round half-even to cents at final aggregation only. Every monetary quantization in the
repo goes through one of the two functions below — nothing else may quantize money.

Floats are rejected outright: money enters the system as ``Decimal``, ``int``, or a
decimal string, never as a binary float.
"""

from decimal import ROUND_HALF_EVEN, Decimal

SIX = Decimal("0.000001")
CENT = Decimal("0.01")
ZERO = Decimal("0")


def _as_decimal(value: Decimal | int | str) -> Decimal:
    if isinstance(value, float):
        raise TypeError("money is never float (BUILD_PLAN §0 invariant 1)")
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int | str):
        return Decimal(value)
    raise TypeError(f"cannot treat {type(value).__name__} as money")


def money6(value: Decimal | int | str) -> Decimal:
    """Quantize a line-level amount to 6 decimal places, half-even."""
    return _as_decimal(value).quantize(SIX, rounding=ROUND_HALF_EVEN)


def to_cents(value: Decimal | int | str) -> Decimal:
    """Round an artist-facing total to cents, half-even. Final aggregation only."""
    return _as_decimal(value).quantize(CENT, rounding=ROUND_HALF_EVEN)
