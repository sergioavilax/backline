"""Retrieval probe plumbing (skips without DATABASE_URL).

Structural checks only — the probe *measures*; quality numbers belong in PHASE_LOG,
not in assertions that would turn honest low scores into red builds. What must hold:
40 well-formed queries with structurally resolved golds, all four modes reported,
metrics in range, deterministic across runs.
"""

import asyncpg
import pytest

from backline.rag.embed import run_embed
from backline.rag.embedder import HashingEmbedder
from backline.rag.reranker import LexicalReranker
from evals.retrieval_probe import N_QUERIES, build_queries, format_report, run_probe
from tests.conftest import WorldEnv, requires_postgres

pytestmark = requires_postgres


@pytest.fixture(autouse=True)
async def ensure_embedded(world_env: WorldEnv, pool: asyncpg.Pool) -> None:
    await run_embed(pool, data_dir=world_env.data_dir, embedder=HashingEmbedder())


async def test_query_set_shape_and_determinism(pool: asyncpg.Pool) -> None:
    queries = await build_queries(pool)
    assert len(queries) == N_QUERIES
    assert len({q.artist_id for q in queries}) == N_QUERIES  # one artist per query
    assert len({q.intent for q in queries}) >= 6  # intent diversity incl. specials
    assert {"minimum_guarantee", "termination"} <= {q.intent for q in queries}
    for query in queries:
        assert query.gold, query.text
        assert all(clause.startswith("§") for _cid, clause in query.gold)
    again = await build_queries(pool)
    assert queries == again


async def test_golds_point_at_governing_documents(pool: asyncpg.Pool) -> None:
    queries = await build_queries(pool)
    for query in queries:
        for contract_id, clause_no in query.gold:
            row = await pool.fetchrow(
                "SELECT kind FROM rag.contract_chunks WHERE contract_id = $1 "
                "AND clause_no = $2 AND part = 0",
                contract_id,
                clause_no,
            )
            assert row is not None, f"gold chunk missing: {contract_id} {clause_no}"
            if clause_no.startswith("§A"):
                assert row["kind"] == "amendment"


async def test_probe_reports_all_modes(pool: asyncpg.Pool) -> None:
    report = await run_probe(pool, embedder=HashingEmbedder(), reranker=LexicalReranker(), top_k=10)
    assert set(report["modes"]) == {
        "scoped/rerank",
        "scoped/fused",
        "unscoped/rerank",
        "unscoped/fused",
    }
    for summary in report["modes"].values():
        for key in ("mrr", "recall@1", "recall@3", "recall@5", "recall@10"):
            assert 0.0 <= summary[key] <= 1.0
    assert len(report["per_query"]) == N_QUERIES
    text = format_report(report)
    assert "rerank lift" in text
    assert report["stack"]["embedder"] == "hash-bow-384-v1"
