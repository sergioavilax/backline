"""The embed build job against the seeded corpus (skips without DATABASE_URL).

DoD line: "embed job idempotent". Hash-keyed: an unchanged corpus re-run touches
nothing; a changed chunk re-embeds exactly that chunk; the ivfflat index exists after
a build with embeddings (built post-bulk-insert per the §9 pitfall).
"""

import asyncpg
import pytest

from backline.rag.embed import run_embed
from backline.rag.embedder import HashingEmbedder
from tests.conftest import WorldEnv, requires_postgres

pytestmark = requires_postgres


@pytest.fixture
async def embedded(world_env: WorldEnv, pool: asyncpg.Pool) -> object:
    report = await run_embed(pool, data_dir=world_env.data_dir, embedder=HashingEmbedder())
    return report


async def test_build_populates_all_contracts(embedded: object, pool: asyncpg.Pool) -> None:
    contracts = await pool.fetchval("SELECT count(DISTINCT contract_id) FROM rag.contract_chunks")
    assert contracts == 385
    chunks = await pool.fetchval("SELECT count(*) FROM rag.contract_chunks")
    assert isinstance(chunks, int) and chunks > 2000  # ~8 clauses x 301 bases + amendments
    unembedded = await pool.fetchval(
        "SELECT count(*) FROM rag.contract_chunks WHERE embedding IS NULL"
    )
    assert unembedded == 0
    models = await pool.fetch("SELECT DISTINCT embedding_model FROM rag.contract_chunks")
    assert [r["embedding_model"] for r in models] == ["hash-bow-384-v1"]


async def test_metadata_matches_contracts(embedded: object, pool: asyncpg.Pool) -> None:
    mismatched = await pool.fetchval(
        """
        SELECT count(*) FROM rag.contract_chunks ch
        JOIN label.contracts c ON c.id = ch.contract_id
        WHERE ch.artist_id != c.artist_id OR ch.kind != c.kind
           OR ch.effective_from != c.effective_from
           OR ch.effective_to IS DISTINCT FROM c.effective_to
        """
    )
    assert mismatched == 0
    # Every base contract carries the full clause skeleton.
    missing_royalties = await pool.fetchval(
        """
        SELECT count(*) FROM label.contracts c
        WHERE c.kind = 'base' AND NOT EXISTS (
            SELECT 1 FROM rag.contract_chunks ch
            WHERE ch.contract_id = c.id AND ch.clause_no = '§3'
        )
        """
    )
    assert missing_royalties == 0


async def test_canary_contract_is_chunked_verbatim(embedded: object, pool: asyncpg.Pool) -> None:
    # The one seeded injection canary (§4.6) lives in §7 of exactly one contract.
    rows = await pool.fetch(
        "SELECT contract_id, content FROM rag.contract_chunks "
        "WHERE content ILIKE '%truth.expected_ledger%'"
    )
    assert len(rows) == 1
    assert rows[0]["contract_id"] == 670  # FBR-C-00670, corpus-only (Phase 1 log)
    assert "SYSTEM OVERRIDE" in rows[0]["content"]


async def test_store_renders_rates_plain_after_real_seed_and_embed(
    embedded: object, pool: asyncpg.Pool
) -> None:
    """D-030: assert against the artifact agents actually read — rag.contract_chunks
    after a real seed + embed — not only the rendered files. A stale corpus re-dirties
    the store through this very job (that is how "1E+1% of Net Receipts" outlived the
    D-029 file-level guard), so the store is the surface the guard must hold on."""
    scientific = await pool.fetch(
        "SELECT contract_id, clause_no, part FROM rag.contract_chunks "
        "WHERE content ~ '[0-9]E[+-][0-9]' OR heading ~ '[0-9]E[+-][0-9]' "
        "ORDER BY contract_id, clause_no, part"
    )
    assert not scientific, (
        f"chunks carry scientific-notation rates (stale corpus? run `make seed && make "
        f"embed`): {[tuple(r) for r in scientific[:5]]}"
    )
    typo = await pool.fetch(
        "SELECT contract_id, clause_no FROM rag.contract_chunks "
        "WHERE content ~ '% percentage points' ORDER BY contract_id"
    )
    assert not typo, f"escalation typo '…% percentage points' in chunks: {typo[:5]}"
    # The wording the fix *does* render must actually be in the store (non-vacuous).
    escalated = await pool.fetchval(
        "SELECT count(*) FROM rag.contract_chunks WHERE content ~ 'percentage points'"
    )
    assert isinstance(escalated, int) and escalated >= 20


async def test_second_run_is_a_noop(
    embedded: object, world_env: WorldEnv, pool: asyncpg.Pool
) -> None:
    report = await run_embed(pool, data_dir=world_env.data_dir, embedder=HashingEmbedder())
    assert report.chunks_inserted == 0
    assert report.chunks_updated == 0
    assert report.chunks_deleted == 0
    assert report.embedded_now == 0


async def test_changed_chunk_reembeds_exactly_that_chunk(
    embedded: object, world_env: WorldEnv, pool: asyncpg.Pool
) -> None:
    # Simulate drift: hand-mangle one stored chunk; the job must restore + re-embed it.
    target = await pool.fetchrow(
        "SELECT contract_id, clause_no, part FROM rag.contract_chunks "
        "ORDER BY contract_id, clause_no, part LIMIT 1"
    )
    await pool.execute(
        "UPDATE rag.contract_chunks SET content = 'tampered', content_hash = 'x', "
        "embedding = NULL, embedding_model = NULL "
        "WHERE contract_id = $1 AND clause_no = $2 AND part = $3",
        target["contract_id"],
        target["clause_no"],
        target["part"],
    )
    report = await run_embed(pool, data_dir=world_env.data_dir, embedder=HashingEmbedder())
    assert report.chunks_updated == 1
    assert report.chunks_inserted == 0
    assert report.embedded_now == 1
    restored = await pool.fetchval(
        "SELECT content FROM rag.contract_chunks "
        "WHERE contract_id = $1 AND clause_no = $2 AND part = $3",
        target["contract_id"],
        target["clause_no"],
        target["part"],
    )
    assert restored != "tampered"


async def test_ivfflat_index_exists_after_embed(embedded: object, pool: asyncpg.Pool) -> None:
    indexdef = await pool.fetchval(
        "SELECT indexdef FROM pg_indexes WHERE schemaname = 'rag' "
        "AND indexname = 'contract_chunks_embedding_idx'"
    )
    assert indexdef is not None and "ivfflat" in indexdef


async def test_chunks_only_build_without_embedder(
    embedded: object, world_env: WorldEnv, pool: asyncpg.Pool
) -> None:
    # --best-effort path: no embedder available → chunks still current, embeddings
    # untouched, and the report says so.
    report = await run_embed(pool, data_dir=world_env.data_dir, embedder=None)
    assert report.embedded_now == 0
    assert report.embedder_id is None
    assert report.chunks_inserted == 0  # corpus unchanged
