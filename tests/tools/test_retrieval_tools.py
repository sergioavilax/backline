"""search_contracts + read_clause tools (skips without DATABASE_URL).

The pipeline itself is covered in tests/rag; here: the agent-facing contract —
structural citations, artist scoping/resolution, as-of dating, and the exact-text
fetch that backs post-retrieval verification.
"""

from datetime import date

import asyncpg
import pytest

from backline.config import get_settings
from backline.rag.embed import run_embed
from backline.rag.embedder import HashingEmbedder
from backline.rag.reranker import LexicalReranker
from backline.tools.context import ToolContext
from backline.tools.retrieval import build_read_clause_tool, build_search_contracts_tool
from tests.conftest import WorldEnv, requires_postgres

pytestmark = requires_postgres


@pytest.fixture(autouse=True)
async def ensure_embedded(world_env: WorldEnv, pool: asyncpg.Pool) -> None:
    await run_embed(pool, data_dir=world_env.data_dir, embedder=HashingEmbedder())


@pytest.fixture
def ctx(pool: asyncpg.Pool) -> ToolContext:
    return ToolContext(
        pool=pool,
        settings=get_settings(),
        embedder=HashingEmbedder(),
        reranker=LexicalReranker(),
    )


async def test_search_returns_structural_citations(ctx: ToolContext, pool: asyncpg.Pool) -> None:
    artist = await pool.fetchrow("SELECT id, stage_name FROM label.artists ORDER BY id LIMIT 1")
    tool = build_search_contracts_tool(ctx)
    out = await tool.handler(
        tool.params(query="royalty rate for streaming", artist=artist["stage_name"])
    )
    assert "§" in out
    assert "FBR-" in out  # citation codes, not vibes
    assert "read_clause" in out  # nudge toward post-retrieval verification


async def test_search_unknown_artist_reports_gracefully(ctx: ToolContext) -> None:
    tool = build_search_contracts_tool(ctx)
    out = await tool.handler(tool.params(query="royalties", artist="Definitely Nobody Real"))
    assert "no artist" in out.lower()


async def test_search_respects_as_of_date(ctx: ToolContext, pool: asyncpg.Pool) -> None:
    case = await pool.fetchrow(
        """
        SELECT c.artist_id, a.amendment_id, c.effective_from,
               (SELECT stage_name FROM label.artists WHERE id = c.artist_id) AS stage_name
        FROM label.amendments a
        JOIN label.contracts c ON c.id = a.amendment_id
        WHERE a.replaced_sections = ARRAY['royalties']
          AND c.effective_from BETWEEN DATE '2025-09-01' AND DATE '2026-05-01'
        ORDER BY a.amendment_id LIMIT 1
        """
    )
    tool = build_search_contracts_tool(ctx)
    amendment_code = f"FBR-A-{case['amendment_id']:05d}"

    late = await tool.handler(
        tool.params(
            query="royalty percentages net receipts streaming",
            artist=case["stage_name"],
            as_of_date=date(2026, 6, 30),
        )
    )
    early = await tool.handler(
        tool.params(
            query="royalty percentages net receipts streaming",
            artist=case["stage_name"],
            as_of_date=case["effective_from"].replace(day=1),
        )
    )
    assert amendment_code in late
    assert amendment_code not in early  # not yet effective on the early date


async def test_read_clause_returns_exact_text(ctx: ToolContext, pool: asyncpg.Pool) -> None:
    row = await pool.fetchrow(
        "SELECT contract_id, content FROM rag.contract_chunks "
        "WHERE clause_no = '§4' AND part = 0 ORDER BY contract_id LIMIT 1"
    )
    tool = build_read_clause_tool(ctx)
    out = await tool.handler(tool.params(contract_id=row["contract_id"], clause_no="§4"))
    assert row["content"] in out  # verbatim, not a summary


async def test_read_clause_accepts_bare_clause_numbers(
    ctx: ToolContext, pool: asyncpg.Pool
) -> None:
    contract_id = await pool.fetchval(
        "SELECT contract_id FROM rag.contract_chunks WHERE clause_no = '§3' LIMIT 1"
    )
    tool = build_read_clause_tool(ctx)
    with_sign = await tool.handler(tool.params(contract_id=contract_id, clause_no="§3"))
    bare = await tool.handler(tool.params(contract_id=contract_id, clause_no="3"))
    assert with_sign == bare


async def test_read_clause_missing_lists_available(ctx: ToolContext, pool: asyncpg.Pool) -> None:
    contract_id = await pool.fetchval(
        "SELECT contract_id FROM rag.contract_chunks WHERE kind = 'base' LIMIT 1"
    )
    tool = build_read_clause_tool(ctx)
    out = await tool.handler(tool.params(contract_id=contract_id, clause_no="§99"))
    assert "no clause" in out.lower()
    assert "§3" in out  # tells the model what exists

    out2 = await tool.handler(tool.params(contract_id=999_999_999, clause_no="§1"))
    assert "no contract" in out2.lower()
