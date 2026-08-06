"""Contract renderer: canonical terms JSON -> numbered-clause PDF + plain-text sidecar.

The PDF is a faithful rendering of ``label.contract_terms`` (never the other way around).
Clause numbering is deterministic (§1 Definitions ... §8 General), so Phase 3's chunker
can split structurally instead of by token windows. ``rl_config.invariant`` pins
ReportLab's timestamps/IDs so the same world produces byte-identical PDFs.

The one seeded injection canary (§4.6) renders into §7 Special Provisions of exactly one
contract — corpus text only, absent from the JSON terms.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import Flowable, Paragraph, SimpleDocTemplate, Spacer

from datagen.config import WorldConfig
from datagen.world import Structure
from datagen.worldmodel import Contract

REVENUE_TYPE_LABEL = {
    "streaming": "interactive audio streaming",
    "download": "permanent digital downloads",
    "physical": "physical product (vinyl, compact disc)",
    "sync": "synchronization licensing",
}

_TITLE = ParagraphStyle(
    "ContractTitle", fontName="Helvetica-Bold", fontSize=14, leading=18, spaceAfter=10
)
_HEADING = ParagraphStyle(
    "ClauseHeading",
    fontName="Helvetica-Bold",
    fontSize=11,
    leading=14,
    spaceBefore=12,
    spaceAfter=4,
)
_BODY = ParagraphStyle("ClauseBody", fontName="Helvetica", fontSize=9.5, leading=13, spaceAfter=4)


def _fmt_date(d: date | None) -> str:
    return d.strftime("%B %d, %Y") if d else "the date of last-dated signature"


def _pct(rate: str) -> str:
    return f"{(Decimal(rate) * 100).normalize()}%"


def _money(amount: str) -> str:
    return f"US ${Decimal(amount):,.2f}"


def contract_document(
    contract: Contract, structure: Structure, config: WorldConfig
) -> list[tuple[str, list[str]]]:
    """The document as (heading, paragraphs) clauses — one source for PDF and .txt."""
    world = structure.world
    artist = next(a for a in world.artists if a.id == contract.artist_id)
    sections = contract.terms_json["sections"]
    label = config.label_name
    code = Path(contract.doc_path).stem.split("_")[0]

    if contract.kind == "amendment":
        return _amendment_document(contract, artist.stage_name, artist.legal_name, label, code)

    royalties = sections["royalties"]
    advances = sections["advances_recoupment"]
    territory = sections.get("term_territory", {"excluded_territories": []})
    excluded = list(territory["excluded_territories"])
    is_xcollat = str(advances["account"]).startswith("XC-")

    clauses: list[tuple[str, list[str]]] = []
    clauses.append(
        (
            f"RECORDING AND ROYALTY AGREEMENT — {code}",
            [
                f"This Recording and Royalty Agreement (the “Agreement”) is entered "
                f"into effective as of {_fmt_date(contract.effective_from)} by and between "
                f"{label}, an independent recording label (“Label”), and "
                f"{artist.legal_name}, professionally known as "
                f"“{artist.stage_name}” (“Artist”).",
            ],
        )
    )
    clauses.append(
        (
            "§1. DEFINITIONS",
            [
                "“Net Receipts” means all monies actually received by Label from the "
                "exploitation of Masters hereunder, after deduction of distribution fees "
                "charged by third-party distributors and mechanical or neighbouring-rights "
                "royalties payable to third parties.",
                "“Masters” means the master recordings embodying Artist's "
                "performances recorded during the Term and delivered to Label under this "
                "Agreement.",
                "“Accounting Period” means each calendar month during which Net "
                "Receipts are received by Label.",
            ],
        )
    )
    term_paragraphs = [
        f"The Term of this Agreement commences on {_fmt_date(contract.effective_from)} and "
        + (
            f"continues until {_fmt_date(contract.effective_to)}, unless earlier terminated "
            "in accordance with this Agreement."
            if contract.effective_to
            else "continues until superseded or terminated in accordance with this Agreement."
        ),
    ]
    if excluded:
        territories = ", ".join(excluded)
        term_paragraphs.append(
            f"The territory of this Agreement is the world EXCLUDING {territories} "
            f"(the “Territory”). For the avoidance of doubt, no royalties accrue "
            f"to Artist on exploitation in {territories}, and Label holds no exploitation "
            f"obligation there."
        )
    else:
        term_paragraphs.append("The territory of this Agreement is the world (the “Territory”).")
    clauses.append(("§2. TERM AND TERRITORY", term_paragraphs))

    rate_paragraphs = [
        "In consideration of the rights granted herein, Label shall credit Artist's "
        "royalty account with the following percentages of Net Receipts:",
    ]
    for entry in royalties["rate_card"]:
        scope = (
            "throughout the Territory"
            if entry["territory"] == "WW"
            else f"in the territory of {entry['territory']}"
        )
        rate_paragraphs.append(
            f"(a{len(rate_paragraphs)}) {_pct(entry['rate'])} of Net Receipts from "
            f"{REVENUE_TYPE_LABEL[entry['revenue_type']]} {scope};"
        )
    for esc in royalties.get("escalators", []):
        rate_paragraphs.append(
            f"Escalation: upon Artist's cumulative Net Receipts under this Agreement first "
            f"exceeding {_money(esc['threshold_usd'])} (measured at the opening of an "
            f"Accounting Period), each rate above shall increase by "
            f"{_pct(esc['bump'])} percentage points for all subsequent Accounting Periods. "
            f"Escalation tiers state total uplifts and do not compound."
        )
    clauses.append(("§3. ROYALTIES", rate_paragraphs))

    recoup_paragraphs = [
        "All advances paid to Artist and all recoupable costs incurred by Label shall be "
        "charged to Artist's recoupment account and recouped from royalties otherwise "
        "payable hereunder before any royalty payment is made.",
        f"Recoupment account designation: {advances['account']}.",
        "Recoupable cost classes under this Agreement: "
        + ", ".join(advances["recoupable_classes"])
        + ". Marketing and promotion costs are expressly non-recoupable.",
    ]
    if advances["minimum_guarantee_per_period"] is not None:
        mg = advances["minimum_guarantee_per_period"]
        recoup_paragraphs.append(
            f"Minimum guarantee: notwithstanding the foregoing, Label shall pay Artist no "
            f"less than {_money(mg)} in respect of each Accounting Period. Any amount paid "
            f"pursuant to this clause in excess of royalties otherwise payable constitutes "
            f"a recoupable advance against future royalties."
        )
    clauses.append(("§4. ADVANCES AND RECOUPMENT", recoup_paragraphs))

    clauses.append(
        (
            "§5. ACCOUNTING",
            [
                "Label shall render accountings monthly, within forty-five (45) days "
                "following the close of each Accounting Period, together with payment of "
                "royalties then due. Royalty computations are carried at six decimal "
                "places; amounts payable to Artist are rounded to the nearest cent "
                "(half-even) at final aggregation only.",
                "Artist may, not more than once per year and upon thirty (30) days' "
                "notice, audit Label's books and records relating to this Agreement.",
            ],
        )
    )

    if is_xcollat:
        clauses.append(
            (
                "§6. CROSS-COLLATERALIZATION",
                [
                    f"This Agreement is cross-collateralized with all other agreements "
                    f"between Label and Artist bearing recoupment account designation "
                    f"{advances['account']}: Net Receipts under any such agreement shall "
                    f"be applied against the combined unrecouped balance of that account.",
                ],
            )
        )
    else:
        clauses.append(
            (
                "§6. CROSS-COLLATERALIZATION",
                [
                    "This Agreement is NOT cross-collateralized: the recoupment account "
                    "designated in §4 stands alone, and royalties hereunder shall not be "
                    "applied against balances arising under any other agreement between "
                    "the parties.",
                ],
            )
        )

    special: list[str] = []
    if contract.effective_to is not None and contract.effective_to <= date(2026, 6, 30):
        special.append(
            f"Termination: this Agreement terminates on {_fmt_date(contract.effective_to)}. "
            f"Net Receipts from exploitation of Masters received after termination remain "
            f"accountable to Artist under the terms hereof (post-term accounting)."
        )
    if contract.has_canary:
        special.append(config.canary_text)
    if not special:
        special.append("No special provisions apply to this Agreement beyond those stated above.")
    clauses.append(("§7. SPECIAL PROVISIONS", special))

    clauses.append(
        (
            "§8. GENERAL",
            [
                "This Agreement constitutes the entire understanding of the parties and "
                "may be amended only by a written instrument executed by both parties "
                "expressly identifying the sections replaced.",
                "This Agreement shall be governed by the laws of the State of New York, "
                "without regard to its conflict-of-laws principles.",
                f"Executed as of {_fmt_date(contract.effective_from)}.",
                f"{label} — Authorized Signatory",
                f"{artist.legal_name} (“{artist.stage_name}”)",
            ],
        )
    )
    return clauses


def _amendment_document(
    contract: Contract, stage_name: str, legal_name: str, label: str, code: str
) -> list[tuple[str, list[str]]]:
    sections = contract.terms_json["sections"]
    replaced = list(contract.replaced_sections)
    base_id = contract.supersedes_contract_id
    if base_id is None:
        raise ValueError(f"amendment {contract.id} has no supersedes_contract_id")
    section_titles = {
        "term_territory": "§2 (Term and Territory)",
        "royalties": "§3 (Royalties)",
        "advances_recoupment": "§4 (Advances and Recoupment)",
    }
    clauses: list[tuple[str, list[str]]] = [
        (
            f"AMENDMENT TO RECORDING AGREEMENT — {code}",
            [
                f"This Amendment, effective as of {_fmt_date(contract.effective_from)}, is "
                f"made between {label} and {legal_name} (“{stage_name}”) and "
                f"amends the Recording and Royalty Agreement FBR-C-{base_id:05d} between "
                f"the parties (the “Base Agreement”).",
                "The sections of the Base Agreement identified below are replaced in "
                "their entirety as of the effective date of this Amendment; all other "
                "provisions of the Base Agreement remain in full force.",
                "Sections replaced: " + ", ".join(section_titles[key] for key in replaced) + ".",
            ],
        )
    ]
    if "royalties" in sections:
        royalties = sections["royalties"]
        paragraphs = ["§3 (Royalties) of the Base Agreement is replaced as follows:"]
        for entry in royalties["rate_card"]:
            scope = (
                "throughout the Territory"
                if entry["territory"] == "WW"
                else f"in the territory of {entry['territory']}"
            )
            paragraphs.append(
                f"{_pct(entry['rate'])} of Net Receipts from "
                f"{REVENUE_TYPE_LABEL[entry['revenue_type']]} {scope};"
            )
        for esc in royalties.get("escalators", []):
            paragraphs.append(
                f"Escalation: upon cumulative Net Receipts first exceeding "
                f"{_money(esc['threshold_usd'])}, each rate increases by "
                f"{_pct(esc['bump'])} percentage points thereafter."
            )
        clauses.append(("§A1. REPLACEMENT ROYALTY PROVISIONS", paragraphs))
    if "advances_recoupment" in sections:
        advances = sections["advances_recoupment"]
        clauses.append(
            (
                "§A2. REPLACEMENT ADVANCES AND RECOUPMENT PROVISIONS",
                [
                    "§4 (Advances and Recoupment) of the Base Agreement is replaced as follows:",
                    f"Recoupment account designation (unchanged): {advances['account']}.",
                    "Recoupable cost classes: "
                    + ", ".join(advances["recoupable_classes"])
                    + ". Marketing and promotion costs remain non-recoupable.",
                ],
            )
        )
    clauses.append(
        (
            "§A9. GENERAL",
            [
                "Except as expressly amended hereby, the Base Agreement is ratified and "
                "confirmed in all respects.",
                f"Executed as of {_fmt_date(contract.effective_from)}.",
                f"{label} — Authorized Signatory",
                f"{legal_name} (“{stage_name}”)",
            ],
        )
    )
    return clauses


def document_text(clauses: list[tuple[str, list[str]]]) -> str:
    parts: list[str] = []
    for heading, paragraphs in clauses:
        parts.append(heading)
        parts.extend(paragraphs)
        parts.append("")
    return "\n".join(parts).rstrip() + "\n"


def render_contract_pdf(clauses: list[tuple[str, list[str]]], out_path: Path) -> None:
    doc = SimpleDocTemplate(
        str(out_path),
        pagesize=LETTER,
        leftMargin=0.9 * inch,
        rightMargin=0.9 * inch,
        topMargin=0.8 * inch,
        bottomMargin=0.8 * inch,
        invariant=1,
        title=out_path.stem,
        author="Foldback Records",
    )
    story: list[Flowable] = []
    first = True
    for heading, paragraphs in clauses:
        style = _TITLE if first else _HEADING
        story.append(Paragraph(heading, style))
        for text in paragraphs:
            story.append(Paragraph(text, _BODY))
        story.append(Spacer(1, 4))
        first = False
    doc.build(story)


def render_all_contracts(structure: Structure, data_dir: Path) -> int:
    """Render every contract to PDF + .txt sidecar. Returns the number rendered."""
    from reportlab import rl_config

    rl_config.invariant = 1
    config = structure.config
    contracts_dir = data_dir / "contracts"
    txt_dir = contracts_dir / "txt"
    txt_dir.mkdir(parents=True, exist_ok=True)
    for contract in structure.world.contracts:
        clauses = contract_document(contract, structure, config)
        # doc_path is the canonical DB-facing relative path ("data/contracts/<file>.pdf");
        # on disk the file lives under data_dir regardless of what data_dir is named.
        pdf_path = contracts_dir / Path(contract.doc_path).name
        render_contract_pdf(clauses, pdf_path)
        (txt_dir / (pdf_path.stem + ".txt")).write_text(document_text(clauses), encoding="utf-8")
    return len(structure.world.contracts)
