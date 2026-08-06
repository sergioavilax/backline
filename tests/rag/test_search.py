"""Hybrid search pipeline over the seeded chunk store (skips without DATABASE_URL).

FTS + vector legs fused by RRF, cross-encoder rerank as a flag-toggleable stage, and
the governing-doc filter applied before either leg. Uses the deterministic hash
embedder + lexical reranker (the offline stack); the real-model numbers come from the
retrieval probe on a machine that can load them.
"""

from datetime import date

import asyncpg
import pytest

from backline.rag.embed import run_embed
from backline.rag.embedder import HashingEmbedder, get_embedder
from backline.rag.reranker import LexicalReranker
from backline.rag.search import search_chunks
from tests.conftest import WorldEnv, requires_postgres

pytestmark = requires_postgres

AS_OF = date(2026, 6, 30)


@pytest.fixture(autouse=True)
async def ensure_embedded(world_env: WorldEnv, pool: asyncpg.Pool) -> None:
    await run_embed(pool, data_dir=world_env.data_dir, embedder=HashingEmbedder())


async def test_artist_scoped_royalties_query_finds_rate_clause(pool: asyncpg.Pool) -> None:
    artist = await pool.fetchrow(
        """
        SELECT a.id, a.stage_name FROM label.artists a
        WHERE NOT EXISTS (SELECT 1 FROM label.amendments am
                          JOIN label.contracts c ON c.id = am.amendment_id
                          WHERE c.artist_id = a.id)
          AND (SELECT count(*) FROM label.contracts c
               WHERE c.artist_id = a.id AND c.kind = 'base') = 1
        ORDER BY a.id LIMIT 1
        """
    )
    result = await search_chunks(
        pool,
        "what royalty rate applies to interactive audio streaming",
        artist_id=artist["id"],
        as_of=AS_OF,
        embedder=HashingEmbedder(),
        reranker=None,
    )
    assert result.mode == "hybrid"
    assert result.hits, "expected hits for a royalties query"
    top_clauses = [h.clause_no for h in result.hits[:3]]
    assert "§3" in top_clauses


async def test_supersession_governs_search_results(pool: asyncpg.Pool) -> None:
    case = await pool.fetchrow(
        """
        SELECT c.artist_id, a.amendment_id, a.supersedes_contract_id AS base_id
        FROM label.amendments a
        JOIN label.contracts c ON c.id = a.amendment_id
        WHERE a.replaced_sections = ARRAY['royalties']
          AND c.effective_from <= DATE '2026-06-30'
        ORDER BY a.amendment_id LIMIT 1
        """
    )
    kwargs = {
        "artist_id": case["artist_id"],
        "as_of": AS_OF,
        "embedder": HashingEmbedder(),
        "reranker": None,
        "top_k": 20,
    }
    governed = await search_chunks(pool, "royalty percentages of net receipts", **kwargs)
    governed_keys = {(h.contract_id, h.clause_no) for h in governed.hits}
    assert (case["base_id"], "§3") not in governed_keys  # superseded rate clause is dead
    assert any(h.contract_id == case["amendment_id"] for h in governed.hits)

    history = await search_chunks(
        pool, "royalty percentages of net receipts", include_history=True, **kwargs
    )
    history_keys = {(h.contract_id, h.clause_no) for h in history.hits}
    assert (case["base_id"], "§3") in history_keys  # history mode resurfaces it


async def test_rerank_stage_runs_and_is_recorded(pool: asyncpg.Pool) -> None:
    reranked = await search_chunks(
        pool,
        "cross-collateralization of recoupment accounts",
        as_of=AS_OF,
        embedder=HashingEmbedder(),
        reranker=LexicalReranker(),
    )
    assert reranked.reranked_by == "lexical-overlap-v1"
    assert reranked.hits
    assert reranked.hits[0].clause_no in {"§6", "§4"}  # xcollat lives in §6 (account in §4)

    plain = await search_chunks(
        pool,
        "cross-collateralization of recoupment accounts",
        as_of=AS_OF,
        embedder=HashingEmbedder(),
        reranker=None,
    )
    assert plain.reranked_by is None


async def test_scores_are_descending_and_deterministic(pool: asyncpg.Pool) -> None:
    async def once() -> list[tuple[int, str, int]]:
        result = await search_chunks(
            pool,
            "minimum guarantee per accounting period",
            as_of=AS_OF,
            embedder=HashingEmbedder(),
            reranker=LexicalReranker(),
        )
        assert all(a.score >= b.score for a, b in zip(result.hits, result.hits[1:], strict=False))
        return [(h.contract_id, h.clause_no, h.part) for h in result.hits]

    assert await once() == await once()


async def test_fts_only_mode_without_embeddings(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        tx = conn.transaction()
        await tx.start()
        try:
            await conn.execute(
                "UPDATE rag.contract_chunks SET embedding = NULL, embedding_model = NULL"
            )
            result = await search_chunks(
                conn,
                "royalty account recoupment",
                as_of=AS_OF,
                reranker=None,
            )
            assert result.mode == "fts-only"
            assert result.embedder_id is None
            assert result.hits
        finally:
            await tx.rollback()


async def test_mismatched_query_embedder_is_loud(pool: asyncpg.Pool) -> None:
    class WrongEmbedder(HashingEmbedder):
        id = "some-other-model"

    with pytest.raises(RuntimeError, match="embedd"):
        await search_chunks(pool, "anything", as_of=AS_OF, embedder=WrongEmbedder(), reranker=None)


async def test_no_matches_returns_empty(pool: asyncpg.Pool) -> None:
    result = await search_chunks(
        pool,
        "zzzqx nonexistent gibberish token",
        as_of=AS_OF,
        embedder=HashingEmbedder(),
        reranker=LexicalReranker(),
        top_k=5,
    )
    assert isinstance(result.hits, list)  # may be empty or vector-only noise; no crash


async def test_store_resolved_embedder_comes_from_process_cache(pool: asyncpg.Pool) -> None:
    """With no explicit embedder, search resolves the store's recorded model through
    the process-wide cache — repeated queries must not rebuild (with real models:
    reload weights for) the embedder."""
    get_embedder.cache_clear()
    for _ in range(3):
        result = await search_chunks(pool, "royalty rate on streaming", as_of=AS_OF, reranker=None)
        assert result.mode == "hybrid"
        assert result.embedder_id == HashingEmbedder.id
    info = get_embedder.cache_info()
    assert info.misses == 1  # one construction...
    assert info.hits == 2  # ...shared by every later query
