"""Rate resolution: rate card lookup with territory fallback, carve-outs, escalators."""

from decimal import Decimal

from backline.royaltycalc.rounding import ZERO
from backline.royaltycalc.terms import REVENUE_TYPES, WORLDWIDE, Terms

ONE = Decimal(1)


def base_rate(terms: Terms, revenue_type: str, territory: str) -> Decimal:
    """Rate card lookup: exact territory beats the ``WW`` fallback; carve-outs and
    uncovered revenue types earn zero."""
    if revenue_type not in REVENUE_TYPES:
        raise ValueError(f"unknown revenue_type {revenue_type!r}")
    if territory in terms.excluded_territories:
        return ZERO
    fallback = ZERO
    for entry in terms.rate_card:
        if entry.revenue_type != revenue_type:
            continue
        if entry.territory == territory:
            return entry.rate
        if entry.territory == WORLDWIDE:
            fallback = entry.rate
    return fallback


def escalator_bump(terms: Terms, cumulative_before: Decimal) -> Decimal:
    """Bump from the highest crossed tier (tiers state total bumps, not increments).

    ``cumulative_before`` is the contract's cumulative gross USD at *period start*: a
    threshold crossed during a period bumps the following periods, never its own.
    """
    bump = ZERO
    for esc in terms.escalators:
        if cumulative_before >= esc.threshold_usd and esc.bump > bump:
            bump = esc.bump
    return bump


def effective_rate(
    terms: Terms, revenue_type: str, territory: str, cumulative_before: Decimal
) -> Decimal:
    """The rate the engine applies to a line: base rate plus escalator, capped at 1.

    A zero base (carve-out or uncovered revenue type) never gets bumped.
    """
    base = base_rate(terms, revenue_type, territory)
    if base <= 0:
        return ZERO
    return min(base + escalator_bump(terms, cumulative_before), ONE)
