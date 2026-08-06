"""Backline royalty math — the single implementation (BUILD_PLAN §0 invariant 2, D-001).

Both the synthetic-world truth engine (datagen) and the runtime ``calc_royalties`` tool
import from here. Nothing else in the repo computes royalties, applies rates, recoups, or
rounds money.
"""

from backline.royaltycalc.engine import (
    AccountOutcome,
    ArtistState,
    LineRoyalty,
    PeriodOutcome,
    RevenueLine,
    compute_artist_period,
)
from backline.royaltycalc.fx import to_usd
from backline.royaltycalc.rates import base_rate, effective_rate, escalator_bump
from backline.royaltycalc.recoupment import RecoupResult, apply_minimum_guarantee, recoup
from backline.royaltycalc.rounding import CENT, SIX, ZERO, money6, to_cents
from backline.royaltycalc.terms import (
    KINDS,
    REVENUE_TYPES,
    SECTION_ADVANCES,
    SECTION_KEYS,
    SECTION_ROYALTIES,
    SECTION_TERM_TERRITORY,
    WORLDWIDE,
    Escalator,
    RateCardEntry,
    Terms,
    TermsDoc,
    doc_to_json,
    parse_terms_doc,
    resolve_terms,
)

__all__ = [
    "CENT",
    "KINDS",
    "REVENUE_TYPES",
    "SECTION_ADVANCES",
    "SECTION_KEYS",
    "SECTION_ROYALTIES",
    "SECTION_TERM_TERRITORY",
    "SIX",
    "WORLDWIDE",
    "ZERO",
    "AccountOutcome",
    "ArtistState",
    "Escalator",
    "LineRoyalty",
    "PeriodOutcome",
    "RateCardEntry",
    "RecoupResult",
    "RevenueLine",
    "Terms",
    "TermsDoc",
    "apply_minimum_guarantee",
    "base_rate",
    "compute_artist_period",
    "doc_to_json",
    "effective_rate",
    "escalator_bump",
    "money6",
    "parse_terms_doc",
    "recoup",
    "resolve_terms",
    "to_cents",
    "to_usd",
]
