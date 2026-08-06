"""Canonical contract-terms JSON: parsing, serialization, and amendment supersession.

The terms JSON stored in ``label.contract_terms`` is the single canonical form; the PDF is
a rendering of it. ``royaltycalc`` owns the interpretation — datagen writes these docs and
the runtime calculator reads them through the same parser (D-001).
"""

import json
from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from backline.royaltycalc.terms import (
    SECTION_ADVANCES,
    SECTION_KEYS,
    SECTION_ROYALTIES,
    SECTION_TERM_TERRITORY,
    doc_to_json,
    parse_terms_doc,
    resolve_terms,
)


def base_doc_json() -> dict[str, Any]:
    return {
        "meta": {
            "contract_id": 101,
            "artist_id": 7,
            "kind": "base",
            "effective_from": "2023-04-01",
            "effective_to": None,
            "replaced_sections": [],
        },
        "sections": {
            "term_territory": {"excluded_territories": []},
            "royalties": {
                "rate_card": [
                    {"revenue_type": "streaming", "territory": "WW", "rate": "0.30"},
                    {"revenue_type": "physical", "territory": "GB", "rate": "0.15"},
                    {"revenue_type": "physical", "territory": "WW", "rate": "0.10"},
                ],
                "escalators": [{"threshold_usd": "250000", "bump": "0.02"}],
            },
            "advances_recoupment": {
                "account": "ACC-7-1",
                "recoupable_classes": ["recording", "video"],
                "minimum_guarantee_per_period": None,
            },
        },
    }


def amendment_json(
    contract_id: int = 501,
    effective_from: str = "2026-01-15",
    rate: str = "0.34",
) -> dict[str, Any]:
    return {
        "meta": {
            "contract_id": contract_id,
            "artist_id": 7,
            "kind": "amendment",
            "effective_from": effective_from,
            "effective_to": None,
            "replaced_sections": ["royalties"],
        },
        "sections": {
            "royalties": {
                "rate_card": [{"revenue_type": "streaming", "territory": "WW", "rate": rate}],
                "escalators": [],
            },
        },
    }


class TestParse:
    def test_parses_meta(self) -> None:
        doc = parse_terms_doc(base_doc_json())
        assert doc.contract_id == 101
        assert doc.artist_id == 7
        assert doc.kind == "base"
        assert doc.effective_from == date(2023, 4, 1)
        assert doc.effective_to is None
        assert doc.replaced_sections == ()

    def test_roundtrip_is_identity(self) -> None:
        original = base_doc_json()
        doc = parse_terms_doc(original)
        assert doc_to_json(doc) == original
        # And the JSON form is json-serializable as-is (Decimals are strings).
        json.dumps(doc_to_json(doc))

    def test_rejects_float_rate(self) -> None:
        bad = base_doc_json()
        bad["sections"]["royalties"]["rate_card"][0]["rate"] = 0.30
        with pytest.raises(TypeError, match="float"):
            parse_terms_doc(bad)

    def test_rejects_unknown_revenue_type(self) -> None:
        bad = base_doc_json()
        bad["sections"]["royalties"]["rate_card"][0]["revenue_type"] = "merch"
        with pytest.raises(ValueError, match="merch"):
            parse_terms_doc(bad)

    def test_rejects_unknown_section(self) -> None:
        bad = base_doc_json()
        bad["sections"]["merchandising"] = {}
        with pytest.raises(ValueError, match="merchandising"):
            parse_terms_doc(bad)

    def test_rejects_unknown_kind(self) -> None:
        bad = base_doc_json()
        bad["meta"]["kind"] = "sidedeal"
        with pytest.raises(ValueError, match="sidedeal"):
            parse_terms_doc(bad)

    def test_rejects_negative_rate(self) -> None:
        bad = base_doc_json()
        bad["sections"]["royalties"]["rate_card"][0]["rate"] = "-0.10"
        with pytest.raises(ValueError, match="rate"):
            parse_terms_doc(bad)

    def test_section_constants(self) -> None:
        assert SECTION_KEYS == (SECTION_TERM_TERRITORY, SECTION_ROYALTIES, SECTION_ADVANCES)


class TestResolve:
    def test_base_only(self) -> None:
        doc = parse_terms_doc(base_doc_json())
        terms = resolve_terms(doc, [], as_of=date(2026, 3, 31))
        assert terms.contract_id == 101
        assert terms.account == "ACC-7-1"
        assert terms.recoupable_classes == frozenset({"recording", "video"})
        assert terms.minimum_guarantee_per_period is None
        rates = {(e.revenue_type, e.territory): e.rate for e in terms.rate_card}
        assert rates[("streaming", "WW")] == Decimal("0.30")
        assert terms.escalators[0].threshold_usd == Decimal("250000")

    def test_amendment_not_yet_effective(self) -> None:
        base = parse_terms_doc(base_doc_json())
        amendment = parse_terms_doc(amendment_json())
        terms = resolve_terms(base, [amendment], as_of=date(2026, 1, 14))
        rates = {(e.revenue_type, e.territory): e.rate for e in terms.rate_card}
        assert rates[("streaming", "WW")] == Decimal("0.30")

    def test_amendment_effective_replaces_section_wholesale(self) -> None:
        base = parse_terms_doc(base_doc_json())
        amendment = parse_terms_doc(amendment_json())
        terms = resolve_terms(base, [amendment], as_of=date(2026, 1, 15))
        rates = {(e.revenue_type, e.territory): e.rate for e in terms.rate_card}
        assert rates[("streaming", "WW")] == Decimal("0.34")
        # Wholesale replacement: the old physical entries are gone, escalator too.
        assert ("physical", "GB") not in rates
        assert terms.escalators == ()
        # Untouched sections carry through.
        assert terms.account == "ACC-7-1"

    def test_later_amendment_wins(self) -> None:
        base = parse_terms_doc(base_doc_json())
        first = parse_terms_doc(
            amendment_json(contract_id=501, effective_from="2025-10-01", rate="0.32")
        )
        second = parse_terms_doc(
            amendment_json(contract_id=502, effective_from="2026-02-01", rate="0.36")
        )
        # Order given should not matter; effective date governs.
        terms = resolve_terms(base, [second, first], as_of=date(2026, 6, 30))
        rates = {(e.revenue_type, e.territory): e.rate for e in terms.rate_card}
        assert rates[("streaming", "WW")] == Decimal("0.36")

        mid = resolve_terms(base, [second, first], as_of=date(2026, 1, 31))
        mid_rates = {(e.revenue_type, e.territory): e.rate for e in mid.rate_card}
        assert mid_rates[("streaming", "WW")] == Decimal("0.32")

    def test_post_term_accounting_keeps_last_governing_terms(self) -> None:
        # A terminated deal still governs revenue occurring after effective_to.
        raw = base_doc_json()
        raw["meta"]["effective_to"] = "2026-01-31"
        base = parse_terms_doc(raw)
        terms = resolve_terms(base, [], as_of=date(2026, 5, 31))
        rates = {(e.revenue_type, e.territory): e.rate for e in terms.rate_card}
        assert rates[("streaming", "WW")] == Decimal("0.30")

    def test_resolve_rejects_amendment_as_base(self) -> None:
        amendment = parse_terms_doc(amendment_json())
        with pytest.raises(ValueError, match="base"):
            resolve_terms(amendment, [], as_of=date(2026, 1, 1))

    def test_resolve_rejects_base_as_amendment(self) -> None:
        base = parse_terms_doc(base_doc_json())
        with pytest.raises(ValueError, match="amendment"):
            resolve_terms(base, [base], as_of=date(2026, 1, 1))

    def test_amendment_missing_replaced_payload_rejected_at_parse(self) -> None:
        bad = amendment_json()
        bad["sections"] = {}
        with pytest.raises(ValueError, match="royalties"):
            parse_terms_doc(bad)


class TestValidation:
    def test_empty_territory_rejected(self) -> None:
        bad = base_doc_json()
        bad["sections"]["royalties"]["rate_card"][0]["territory"] = ""
        with pytest.raises(ValueError, match="territory"):
            parse_terms_doc(bad)

    def test_rate_above_one_rejected(self) -> None:
        bad = base_doc_json()
        bad["sections"]["royalties"]["rate_card"][0]["rate"] = "1.01"
        with pytest.raises(ValueError, match="rate"):
            parse_terms_doc(bad)

    def test_duplicate_rate_entry_rejected(self) -> None:
        bad = base_doc_json()
        card = bad["sections"]["royalties"]["rate_card"]
        card.append(dict(card[0]))
        with pytest.raises(ValueError, match="duplicate"):
            parse_terms_doc(bad)

    def test_negative_escalator_threshold_rejected(self) -> None:
        bad = base_doc_json()
        bad["sections"]["royalties"]["escalators"][0]["threshold_usd"] = "-1"
        with pytest.raises(ValueError, match="threshold"):
            parse_terms_doc(bad)

    def test_negative_escalator_bump_rejected(self) -> None:
        bad = base_doc_json()
        bad["sections"]["royalties"]["escalators"][0]["bump"] = "-0.01"
        with pytest.raises(ValueError, match="bump"):
            parse_terms_doc(bad)

    def test_non_string_excluded_territory_rejected(self) -> None:
        bad = base_doc_json()
        bad["sections"]["term_territory"]["excluded_territories"] = [7]
        with pytest.raises(ValueError, match="excluded_territories"):
            parse_terms_doc(bad)

    def test_empty_account_rejected(self) -> None:
        bad = base_doc_json()
        bad["sections"]["advances_recoupment"]["account"] = ""
        with pytest.raises(ValueError, match="account"):
            parse_terms_doc(bad)

    def test_non_string_recoupable_class_rejected(self) -> None:
        bad = base_doc_json()
        bad["sections"]["advances_recoupment"]["recoupable_classes"] = [1]
        with pytest.raises(ValueError, match="recoupable_classes"):
            parse_terms_doc(bad)

    def test_negative_minimum_guarantee_rejected(self) -> None:
        bad = base_doc_json()
        bad["sections"]["advances_recoupment"]["minimum_guarantee_per_period"] = "-100"
        with pytest.raises(ValueError, match="minimum_guarantee"):
            parse_terms_doc(bad)

    def test_base_with_replaced_sections_rejected(self) -> None:
        bad = base_doc_json()
        bad["meta"]["replaced_sections"] = ["royalties"]
        with pytest.raises(ValueError, match="base contracts do not replace"):
            parse_terms_doc(bad)

    def test_base_missing_required_section_rejected(self) -> None:
        bad = base_doc_json()
        del bad["sections"]["advances_recoupment"]
        with pytest.raises(ValueError, match="advances_recoupment"):
            parse_terms_doc(bad)

    def test_amendment_with_empty_replaced_sections_rejected(self) -> None:
        bad = amendment_json()
        bad["meta"]["replaced_sections"] = []
        with pytest.raises(ValueError, match="replaced_sections"):
            parse_terms_doc(bad)

    def test_amendment_unknown_replaced_section_rejected(self) -> None:
        bad = amendment_json()
        bad["meta"]["replaced_sections"] = ["appendix"]
        with pytest.raises(ValueError, match="appendix"):
            parse_terms_doc(bad)

    def test_amendment_extra_section_rejected(self) -> None:
        bad = amendment_json()
        bad["sections"]["term_territory"] = {"excluded_territories": []}
        with pytest.raises(ValueError, match="match replaced_sections"):
            parse_terms_doc(bad)

    def test_decimal_field_accepts_int_and_decimal(self) -> None:
        raw = base_doc_json()
        raw["sections"]["royalties"]["escalators"][0]["threshold_usd"] = 250000
        doc = parse_terms_doc(raw)
        terms = resolve_terms(doc, [], as_of=date(2026, 1, 1))
        assert terms.escalators[0].threshold_usd == Decimal("250000")

    def test_decimal_field_accepts_decimal_instance(self) -> None:
        # In-memory docs (datagen builds them before serializing) may carry Decimals.
        raw = base_doc_json()
        raw["sections"]["royalties"]["rate_card"][0]["rate"] = Decimal("0.31")
        doc = parse_terms_doc(raw)
        terms = resolve_terms(doc, [], as_of=date(2026, 1, 1))
        rates = {(e.revenue_type, e.territory): e.rate for e in terms.rate_card}
        assert rates[("streaming", "WW")] == Decimal("0.31")

    def test_decimal_field_rejects_garbage_string(self) -> None:
        bad = base_doc_json()
        bad["sections"]["royalties"]["rate_card"][0]["rate"] = "thirty percent"
        with pytest.raises(ValueError, match="not a decimal"):
            parse_terms_doc(bad)

    def test_decimal_field_rejects_non_scalar(self) -> None:
        bad = base_doc_json()
        bad["sections"]["royalties"]["rate_card"][0]["rate"] = ["0.30"]
        with pytest.raises(TypeError, match="list"):
            parse_terms_doc(bad)
