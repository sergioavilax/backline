"""FX normalization to USD via the world's fixed monthly rate table."""

from collections.abc import Mapping
from decimal import Decimal

from backline.royaltycalc.rounding import money6


def to_usd(amount: Decimal | int | str, currency: str, usd_rates: Mapping[str, Decimal]) -> Decimal:
    """Convert ``amount`` in ``currency`` to USD at the period's fixed rate, 6dp half-even.

    ``usd_rates`` maps currency code -> USD per 1 unit (so ``usd_rates["USD"] == 1``).
    """
    rate = usd_rates.get(currency)
    if rate is None:
        raise ValueError(f"no FX rate for currency {currency!r}")
    return money6(money6(amount) * rate)
