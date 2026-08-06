"""Recoupment waterfall primitives.

Advances and recoupable expenses accrue to an account balance; period earnings recoup the
balance before anything is payable. A minimum-guarantee clause tops the payable up to the
guarantee, and the top-up itself becomes recoupable balance (it is an advance against
future royalties).
"""

from dataclasses import dataclass
from decimal import Decimal

from backline.royaltycalc.rounding import ZERO


@dataclass(frozen=True)
class RecoupResult:
    recouped: Decimal
    payable_raw: Decimal
    balance_after: Decimal


def recoup(earnings: Decimal, balance_open: Decimal) -> RecoupResult:
    """Apply period earnings against an open balance; both must be non-negative."""
    if earnings < 0:
        raise ValueError(f"earnings must be >= 0, got {earnings}")
    if balance_open < 0:
        raise ValueError(f"balance must be >= 0, got {balance_open}")
    recouped = min(earnings, balance_open)
    return RecoupResult(
        recouped=recouped,
        payable_raw=earnings - recouped,
        balance_after=balance_open - recouped,
    )


def apply_minimum_guarantee(
    payable_raw: Decimal, minimum_guarantee: Decimal | None
) -> tuple[Decimal, Decimal]:
    """Return ``(payable, topup)``: payable lifted to the guarantee, topup recoupable."""
    if minimum_guarantee is None:
        return payable_raw, ZERO
    if minimum_guarantee < 0:
        raise ValueError(f"minimum guarantee must be >= 0, got {minimum_guarantee}")
    if payable_raw < minimum_guarantee:
        return minimum_guarantee, minimum_guarantee - payable_raw
    return payable_raw, ZERO
