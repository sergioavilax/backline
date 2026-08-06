"""Governing-document resolution (D-002) against the seeded world.

The filter is *structural*: SQL over contracts/amendments decides which documents
govern an artist as of a date — and which clauses of the base are dead because an
effective amendment replaced their section — before any text search runs.
"""

from datetime import date, timedelta

import asyncpg
import pytest

from backline.rag.governing import SECTION_CLAUSE, governing_docs
from tests.conftest import requires_postgres

pytestmark = requires_postgres

WINDOW_END = date(2026, 6, 30)


@pytest.fixture
async def amended_case(pool: asyncpg.Pool) -> asyncpg.Record:
    """An artist with exactly one amendment, replacing 'royalties', effective in-window."""
    row = await pool.fetchrow(
        """
        SELECT c.artist_id, a.amendment_id, a.supersedes_contract_id AS base_id,
               c.effective_from
        FROM label.amendments a
        JOIN label.contracts c ON c.id = a.amendment_id
        WHERE a.replaced_sections = ARRAY['royalties']
          AND c.effective_from BETWEEN DATE '2025-08-15' AND DATE '2026-05-01'
          AND (SELECT count(*) FROM label.amendments a2
               JOIN label.contracts c2 ON c2.id = a2.amendment_id
               WHERE c2.artist_id = c.artist_id) = 1
        ORDER BY a.amendment_id
        LIMIT 1
        """
    )
    assert row is not None, "world should contain a single-amendment royalties case"
    return row


async def test_effective_amendment_supersedes_base_royalties(
    pool: asyncpg.Pool, amended_case: asyncpg.Record
) -> None:
    docs = await governing_docs(pool, artist_id=amended_case["artist_id"], as_of=WINDOW_END)
    by_id = {d.contract_id: d for d in docs}
    assert amended_case["amendment_id"] in by_id
    base = by_id[amended_case["base_id"]]
    assert "§3" in base.excluded_clauses  # royalties clause replaced
    assert "§2" not in base.excluded_clauses
    assert "§4" not in base.excluded_clauses


async def test_amendment_not_yet_effective_is_absent(
    pool: asyncpg.Pool, amended_case: asyncpg.Record
) -> None:
    day_before = amended_case["effective_from"] - timedelta(days=1)
    docs = await governing_docs(pool, artist_id=amended_case["artist_id"], as_of=day_before)
    by_id = {d.contract_id: d for d in docs}
    assert amended_case["amendment_id"] not in by_id
    base = by_id[amended_case["base_id"]]
    assert base.excluded_clauses == ()


async def test_terminated_deal_still_governs_post_term(pool: asyncpg.Pool) -> None:
    # Post-term accounting (D-003): a base whose effective_to has passed still governs
    # its era's recordings, so it stays in the governing set.
    row = await pool.fetchrow(
        """
        SELECT id, artist_id, effective_to FROM label.contracts
        WHERE kind = 'base' AND effective_to IS NOT NULL AND effective_to < DATE '2026-06-30'
        ORDER BY id LIMIT 1
        """
    )
    assert row is not None
    docs = await governing_docs(pool, artist_id=row["artist_id"], as_of=WINDOW_END)
    assert row["id"] in {d.contract_id for d in docs}


async def test_multi_era_artist_keeps_all_effective_bases(pool: asyncpg.Pool) -> None:
    row = await pool.fetchrow(
        """
        SELECT artist_id, count(*) AS n FROM label.contracts
        WHERE kind = 'base' GROUP BY artist_id HAVING count(*) >= 2
        ORDER BY artist_id LIMIT 1
        """
    )
    assert row is not None
    docs = await governing_docs(pool, artist_id=row["artist_id"], as_of=WINDOW_END)
    bases = [d for d in docs if d.kind == "base"]
    assert len(bases) == row["n"]


async def test_global_governing_set_spans_all_artists(pool: asyncpg.Pool) -> None:
    docs = await governing_docs(pool, artist_id=None, as_of=WINDOW_END)
    n_contracts_effective = await pool.fetchval(
        "SELECT count(*) FROM label.contracts WHERE effective_from <= DATE '2026-06-30'"
    )
    assert len(docs) == n_contracts_effective


def test_section_clause_mapping_matches_renderer() -> None:
    # pdfrender puts term_territory in §2, royalties in §3, advances in §4; the
    # supersession exclusions must target exactly those clause numbers.
    assert SECTION_CLAUSE == {
        "term_territory": "§2",
        "royalties": "§3",
        "advances_recoupment": "§4",
    }
