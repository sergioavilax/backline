"""Canonical contract terms: the JSON stored in ``label.contract_terms`` and its meaning.

One interpretation, owned here (D-001): datagen writes these docs, the truth engine and the
runtime calculator tool read them back through this parser. The PDF contract is a rendering
of this JSON, never the other way around.

Terms JSON layout (all Decimal quantities are *strings* — floats are rejected):

    {
      "meta": {"contract_id": 101, "artist_id": 7, "kind": "base",
               "effective_from": "2023-04-01", "effective_to": null,
               "replaced_sections": []},
      "sections": {
        "term_territory":      {"excluded_territories": ["JP"]},
        "royalties":           {"rate_card": [{"revenue_type": "streaming",
                                               "territory": "WW", "rate": "0.30"}],
                                "escalators": [{"threshold_usd": "250000", "bump": "0.02"}]},
        "advances_recoupment": {"account": "ACC-7-1",
                                "recoupable_classes": ["recording", "video"],
                                "minimum_guarantee_per_period": null}
      }
    }

Amendments carry ``kind == "amendment"``, a non-empty ``replaced_sections`` list, and
exactly those sections as payload. Supersession is wholesale per section: an effective
amendment replaces the named section entirely (rate card *and* escalators for
``royalties``). Escalator tiers state *total* bumps — the highest crossed tier applies,
tiers are not additive.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

REVENUE_TYPES = frozenset({"streaming", "download", "physical", "sync"})
WORLDWIDE = "WW"

SECTION_TERM_TERRITORY = "term_territory"
SECTION_ROYALTIES = "royalties"
SECTION_ADVANCES = "advances_recoupment"
SECTION_KEYS = (SECTION_TERM_TERRITORY, SECTION_ROYALTIES, SECTION_ADVANCES)

KINDS = frozenset({"base", "amendment"})


@dataclass(frozen=True)
class RateCardEntry:
    revenue_type: str
    territory: str  # ISO-2 country code or "WW" (rest-of-world fallback)
    rate: Decimal


@dataclass(frozen=True)
class Escalator:
    threshold_usd: Decimal  # cumulative contract gross (USD) at period start
    bump: Decimal  # total bump for this tier, absolute points (0.02 = +2 pts)


@dataclass(frozen=True)
class TermsDoc:
    """One parsed contract document (base or amendment), sections kept in raw JSON form."""

    contract_id: int
    artist_id: int
    kind: str
    effective_from: date
    effective_to: date | None
    replaced_sections: tuple[str, ...]
    sections: Mapping[str, Any]


@dataclass(frozen=True)
class Terms:
    """The resolved, engine-facing view of a contract as of a date."""

    contract_id: int
    artist_id: int
    account: str  # recoupment account key (== label.recoup_accounts.xcollat_group_id)
    rate_card: tuple[RateCardEntry, ...]
    escalators: tuple[Escalator, ...]  # sorted by threshold ascending
    excluded_territories: frozenset[str]
    recoupable_classes: frozenset[str]
    minimum_guarantee_per_period: Decimal | None


def _dec(value: object, field: str) -> Decimal:
    if isinstance(value, float):
        raise TypeError(f"{field}: decimal quantities must be strings in terms JSON, not float")
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int | str):
        try:
            return Decimal(value)
        except InvalidOperation as exc:
            raise ValueError(f"{field}: not a decimal: {value!r}") from exc
    raise TypeError(f"{field}: cannot read {type(value).__name__} as Decimal")


def _validate_royalties(section: Mapping[str, Any]) -> None:
    seen: set[tuple[str, str]] = set()
    for entry in section["rate_card"]:
        revenue_type = entry["revenue_type"]
        if revenue_type not in REVENUE_TYPES:
            raise ValueError(f"unknown revenue_type {revenue_type!r}")
        territory = entry["territory"]
        if not isinstance(territory, str) or not territory:
            raise ValueError(f"bad territory {territory!r}")
        rate = _dec(entry["rate"], "rate")
        if rate < 0 or rate > 1:
            raise ValueError(f"rate out of range [0, 1]: {rate}")
        key = (revenue_type, territory)
        if key in seen:
            raise ValueError(f"duplicate rate card entry for {key}")
        seen.add(key)
    for esc in section.get("escalators", []):
        if _dec(esc["threshold_usd"], "threshold_usd") < 0:
            raise ValueError("escalator threshold_usd must be >= 0")
        if _dec(esc["bump"], "bump") < 0:
            raise ValueError("escalator bump must be >= 0")


def _validate_section(key: str, section: Mapping[str, Any]) -> None:
    if key == SECTION_TERM_TERRITORY:
        excluded = section.get("excluded_territories", [])
        if not all(isinstance(t, str) for t in excluded):
            raise ValueError("excluded_territories must be strings")
    elif key == SECTION_ROYALTIES:
        _validate_royalties(section)
    elif key == SECTION_ADVANCES:
        account = section["account"]
        if not isinstance(account, str) or not account:
            raise ValueError(f"bad recoupment account {account!r}")
        if not all(isinstance(c, str) for c in section["recoupable_classes"]):
            raise ValueError("recoupable_classes must be strings")
        mg = section.get("minimum_guarantee_per_period")
        if mg is not None and _dec(mg, "minimum_guarantee_per_period") < 0:
            raise ValueError("minimum_guarantee_per_period must be >= 0")
    else:  # pragma: no cover - callers filter keys first
        raise ValueError(f"unknown section {key!r}")


def parse_terms_doc(obj: Mapping[str, Any]) -> TermsDoc:
    """Parse and validate one canonical terms JSON document (base or amendment)."""
    meta = obj["meta"]
    kind = meta["kind"]
    if kind not in KINDS:
        raise ValueError(f"unknown contract kind {kind!r}")

    sections: Mapping[str, Any] = obj["sections"]
    for key in sections:
        if key not in SECTION_KEYS:
            raise ValueError(f"unknown section {key!r}")
        _validate_section(key, sections[key])

    replaced = tuple(meta.get("replaced_sections") or ())
    if kind == "base":
        if replaced:
            raise ValueError("base contracts do not replace sections")
        for required in (SECTION_ROYALTIES, SECTION_ADVANCES):
            if required not in sections:
                raise ValueError(f"base contract missing required section {required!r}")
    else:
        if not replaced:
            raise ValueError("amendment must name replaced_sections")
        for key in replaced:
            if key not in SECTION_KEYS:
                raise ValueError(f"unknown replaced section {key!r}")
            if key not in sections:
                raise ValueError(f"amendment replaces {key!r} but carries no such section")
        if set(sections) != set(replaced):
            raise ValueError("amendment sections must match replaced_sections exactly")

    effective_to_raw = meta.get("effective_to")
    return TermsDoc(
        contract_id=int(meta["contract_id"]),
        artist_id=int(meta["artist_id"]),
        kind=kind,
        effective_from=date.fromisoformat(meta["effective_from"]),
        effective_to=None if effective_to_raw is None else date.fromisoformat(effective_to_raw),
        replaced_sections=replaced,
        sections=copy.deepcopy(dict(sections)),
    )


def doc_to_json(doc: TermsDoc) -> dict[str, Any]:
    """Serialize back to the canonical JSON form (inverse of :func:`parse_terms_doc`)."""
    return {
        "meta": {
            "contract_id": doc.contract_id,
            "artist_id": doc.artist_id,
            "kind": doc.kind,
            "effective_from": doc.effective_from.isoformat(),
            "effective_to": None if doc.effective_to is None else doc.effective_to.isoformat(),
            "replaced_sections": list(doc.replaced_sections),
        },
        "sections": copy.deepcopy(dict(doc.sections)),
    }


def resolve_terms(base: TermsDoc, amendments: Sequence[TermsDoc], as_of: date) -> Terms:
    """Merge a base contract with its effective amendments into the governing terms.

    Amendments effective on or before ``as_of`` apply in effective-date order (contract id
    breaks ties); each replaces its named sections wholesale. ``base.effective_to`` is
    deliberately ignored: a terminated deal still governs post-term revenue accounting.
    """
    if base.kind != "base":
        raise ValueError(f"contract {base.contract_id} is not a base contract")

    sections: dict[str, Any] = dict(base.sections)
    applicable = sorted(
        (a for a in amendments if a.effective_from <= as_of),
        key=lambda a: (a.effective_from, a.contract_id),
    )
    for amendment in applicable:
        if amendment.kind != "amendment":
            raise ValueError(f"contract {amendment.contract_id} is not an amendment")
        for key in amendment.replaced_sections:
            sections[key] = amendment.sections[key]

    territory = sections.get(SECTION_TERM_TERRITORY, {})
    royalties = sections[SECTION_ROYALTIES]
    advances = sections[SECTION_ADVANCES]

    rate_card = tuple(
        RateCardEntry(
            revenue_type=entry["revenue_type"],
            territory=entry["territory"],
            rate=_dec(entry["rate"], "rate"),
        )
        for entry in royalties["rate_card"]
    )
    escalators = tuple(
        sorted(
            (
                Escalator(
                    threshold_usd=_dec(esc["threshold_usd"], "threshold_usd"),
                    bump=_dec(esc["bump"], "bump"),
                )
                for esc in royalties.get("escalators", [])
            ),
            key=lambda e: e.threshold_usd,
        )
    )
    mg_raw = advances.get("minimum_guarantee_per_period")
    return Terms(
        contract_id=base.contract_id,
        artist_id=base.artist_id,
        account=advances["account"],
        rate_card=rate_card,
        escalators=escalators,
        excluded_territories=frozenset(territory.get("excluded_territories", [])),
        recoupable_classes=frozenset(advances["recoupable_classes"]),
        minimum_guarantee_per_period=(
            None if mg_raw is None else _dec(mg_raw, "minimum_guarantee_per_period")
        ),
    )
