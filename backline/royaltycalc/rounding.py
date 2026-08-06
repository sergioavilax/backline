"""The single rounding + display policy for money and rates (BUILD_PLAN §0, invariant 1).

Line-level amounts keep 6 decimal places (streaming micro-payments); artist-facing totals
round half-even to cents at final aggregation only. Every monetary quantization in the
repo goes through one of the two quantizers below — nothing else may quantize money.

Rate *display* is a policy for the same reason quantization is (D-029/D-030): a royalty
rate stored as ``'0.1'`` must render as ``10``, never ``1E+1`` — ``Decimal.normalize()``
alone reduces exactly the whole-ten percentages to scientific notation. Every renderer
of a rate-as-percentage (contract corpus, calculator output, demo transcript) goes
through ``pct``/``pct_points`` — nothing else may format a rate.

Floats are rejected outright: money enters the system as ``Decimal``, ``int``, or a
decimal string, never as a binary float — and the same applies to rates.
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


def pct_points(rate: Decimal | int | str) -> str:
    """A rate fraction as percentage points: '0.1' → '10', '0.225' → '22.5'.

    ``normalize()`` strips trailing zeros; ``:f`` forbids the scientific notation it
    would otherwise introduce for whole tens (``Decimal('0.1') * 100`` → ``1E+1``).
    Bare number, no sign — escalator prose appends its own unit ("percentage points").
    """
    return f"{(_as_decimal(rate) * 100).normalize():f}"


def pct(rate: Decimal | int | str) -> str:
    """A rate fraction as a display percentage: '0.1' → '10%', '0.225' → '22.5%'."""
    return f"{pct_points(rate)}%"
