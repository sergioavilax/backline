"""search_contracts + read_clause tools (skips without DATABASE_URL).

The pipeline itself is covered in tests/rag; here: the agent-facing contract —
structural citations, artist scoping/resolution, as-of dating, and the exact-text
fetch that backs post-retrieval verification.
"""

from datetime import date, timedelta

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


async def test_search_lists_every_era_governing_document(
    ctx: ToolContext, pool: asyncpg.Pool
) -> None:
    """Multi-era artists get a full governing-document inventory in every artist-scoped
    result — terminated era deals keep governing their recordings (D-003), so hiding
    them behind ranking produced false abstentions (Phase 6 verification, finding 1)."""
    artist = await pool.fetchrow(
        """
        SELECT c.artist_id, a.stage_name
        FROM label.contracts c JOIN label.artists a ON a.id = c.artist_id
        WHERE c.kind = 'base'
        GROUP BY c.artist_id, a.stage_name HAVING count(*) >= 3
        ORDER BY c.artist_id LIMIT 1
        """
    )
    codes = [
        f"FBR-C-{r['id']:05d}"
        for r in await pool.fetch(
            "SELECT id FROM label.contracts WHERE artist_id = $1 AND kind = 'base' ORDER BY id",
            artist["artist_id"],
        )
    ]
    tool = build_search_contracts_tool(ctx)
    out = await tool.handler(
        tool.params(query="royalty rate", artist=artist["stage_name"], as_of_date=date(2026, 6, 30))
    )
    assert "Governing documents" in out
    for code in codes:
        assert code in out, f"era base {code} missing from the governing inventory"


async def test_search_surfaces_terminated_era_sync_terms(
    ctx: ToolContext, pool: asyncpg.Pool
) -> None:
    """The Beatriz Romano regression: an artist whose *terminated* era carries a sync
    rate the current era lacks. The result must name the old era's contract in the
    inventory, and the query-aware snippet must show the sync line itself."""
    case = await pool.fetchrow(
        """
        SELECT old.id AS old_id, old.artist_id, a.stage_name
        FROM label.contracts old
        JOIN label.contract_terms ot ON ot.contract_id = old.id
        JOIN label.artists a ON a.id = old.artist_id
        WHERE old.kind = 'base' AND old.effective_to IS NOT NULL
          AND ot.terms -> 'sections' -> 'royalties' -> 'rate_card'
              @> '[{"revenue_type": "sync"}]'::jsonb
          AND NOT EXISTS (
            SELECT 1 FROM label.contracts cur
            JOIN label.contract_terms ct ON ct.contract_id = cur.id
            WHERE cur.artist_id = old.artist_id AND cur.kind = 'base'
              AND cur.effective_to IS NULL
              AND ct.terms -> 'sections' -> 'royalties' -> 'rate_card'
                  @> '[{"revenue_type": "sync"}]'::jsonb
          )
        ORDER BY old.id LIMIT 1
        """
    )
    assert case is not None, "world should contain a terminated-era-only sync artist"
    tool = build_search_contracts_tool(ctx)
    out = await tool.handler(
        tool.params(query="sync rate", artist=case["stage_name"], as_of_date=date(2026, 6, 30))
    )
    assert f"FBR-C-{case['old_id']:05d}" in out  # the era that answers the question
    assert "synchronization" in out  # visible in a snippet, not hidden past char 240


async def test_search_no_governing_documents_says_so(ctx: ToolContext, pool: asyncpg.Pool) -> None:
    artist = await pool.fetchrow(
        """
        SELECT a.stage_name, min(c.effective_from) AS first
        FROM label.artists a JOIN label.contracts c ON c.artist_id = a.id
        GROUP BY a.id, a.stage_name ORDER BY a.id LIMIT 1
        """
    )
    tool = build_search_contracts_tool(ctx)
    out = await tool.handler(
        tool.params(
            query="royalty rate",
            artist=artist["stage_name"],
            as_of_date=artist["first"] - timedelta(days=1),
        )
    )
    assert "No documents govern" in out


async def test_read_clause_notes_supersession(ctx: ToolContext, pool: asyncpg.Pool) -> None:
    """Reading a base §3 that an effective amendment replaced must say so — Counsel
    cited a dead clause as governing during the Phase 6 verification run."""
    case = await pool.fetchrow(
        """
        SELECT a.supersedes_contract_id AS base_id, a.amendment_id
        FROM label.amendments a
        WHERE a.replaced_sections @> ARRAY['royalties']
        ORDER BY a.amendment_id LIMIT 1
        """
    )
    tool = build_read_clause_tool(ctx)
    out = await tool.handler(tool.params(contract_id=case["base_id"], clause_no="§3"))
    assert f"FBR-A-{case['amendment_id']:05d}" in out
    assert "replaced" in out.lower()

    untouched = await tool.handler(tool.params(contract_id=case["base_id"], clause_no="§1"))
    assert "replaced" not in untouched.lower()  # only superseded clauses get the note


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
