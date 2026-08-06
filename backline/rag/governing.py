"""The governing-document filter (§4.4, D-002): structure first, vectors second.

Which documents govern an artist as of a date is a *relational* fact — base contracts
effective by then (termination does not un-govern: post-term accounting, D-003), plus
amendments effective by then, minus the base clauses those amendments replaced
(wholesale per section, mapped to the renderer's clause numbers). This module resolves
that fact in SQL so retrieval only ever ranks text that can actually answer a
"what applies?" question; history is opt-in at the search layer.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import asyncpg

SECTION_CLAUSE = {
    "term_territory": "§2",
    "royalties": "§3",
    "advances_recoupment": "§4",
}
"""Terms-JSON section → the base-contract clause it renders to (see datagen/pdfrender)."""


@dataclass(frozen=True)
class GoverningDoc:
    contract_id: int
    kind: str  # base | amendment
    excluded_clauses: tuple[str, ...]  # base clauses replaced by an effective amendment


async def governing_docs(
    source: asyncpg.Pool | asyncpg.Connection,
    *,
    artist_id: int | None,
    as_of: date,
) -> list[GoverningDoc]:
    """All documents governing ``artist_id`` (or every artist) as of ``as_of``."""
    rows = await source.fetch(
        """
        SELECT c.id, c.kind, a.supersedes_contract_id, a.replaced_sections
        FROM label.contracts c
        LEFT JOIN label.amendments a ON a.amendment_id = c.id
        WHERE c.effective_from <= $1
          AND ($2::bigint IS NULL OR c.artist_id = $2)
        ORDER BY c.id
        """,
        as_of,
        artist_id,
    )
    excluded: dict[int, set[str]] = {}
    for row in rows:
        if row["kind"] == "amendment" and row["supersedes_contract_id"] is not None:
            target = excluded.setdefault(row["supersedes_contract_id"], set())
            target.update(
                SECTION_CLAUSE[section]
                for section in row["replaced_sections"]
                if section in SECTION_CLAUSE
            )
    return [
        GoverningDoc(
            contract_id=row["id"],
            kind=row["kind"],
            excluded_clauses=tuple(sorted(excluded.get(row["id"], ()))),
        )
        for row in rows
    ]
