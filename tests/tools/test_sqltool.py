"""sql_query tool against the seeded world (skips without DATABASE_URL).

The parse-level policy has its own keyless matrix (test_sqlpolicy); these tests cover
what needs a database: execution + result rendering, Decimal fidelity, the EXPLAIN
cost ceiling, and the read-only transaction backstop.
"""

import asyncpg
import pytest

from backline.config import get_settings
from backline.tools.context import ToolContext
from backline.tools.sqltool import build_sql_query_tool
from tests.conftest import requires_postgres

pytestmark = requires_postgres


@pytest.fixture
def ctx(pool: asyncpg.Pool) -> ToolContext:
    return ToolContext(pool=pool, settings=get_settings())


async def run_query(ctx: ToolContext, query: str) -> str:
    tool = build_sql_query_tool(ctx)
    return await tool.handler(tool.params(query=query))


async def test_simple_select_renders_table(ctx: ToolContext) -> None:
    out = await run_query(ctx, "SELECT count(*) AS n_artists FROM label.artists")
    assert "n_artists" in out
    assert "150" in out
    assert "1 row" in out


async def test_limit_injection_caps_rows_and_is_reported(ctx: ToolContext) -> None:
    out = await run_query(ctx, "SELECT id FROM label.statement_lines")
    assert "200 rows" in out
    assert "LIMIT 200" in out  # the note tells the model why the result stops


async def test_decimal_amounts_render_exactly(ctx: ToolContext) -> None:
    out = await run_query(
        ctx,
        "SELECT gross_amount FROM label.statement_lines WHERE gross_amount > 0 ORDER BY id LIMIT 1",
    )
    row = await ctx.pool.fetchval(
        "SELECT gross_amount FROM label.statement_lines WHERE gross_amount > 0 ORDER BY id LIMIT 1"
    )
    assert str(row) in out  # native Decimal, 6dp, never float-mangled


async def test_null_renders_as_empty(ctx: ToolContext) -> None:
    out = await run_query(
        ctx, "SELECT effective_to FROM label.contracts WHERE effective_to IS NULL LIMIT 1"
    )
    assert "1 row" in out


async def test_policy_violation_surfaces_as_tool_error(ctx: ToolContext) -> None:
    from backline.tools.sqlpolicy import SqlPolicyViolation

    with pytest.raises(SqlPolicyViolation, match="truth"):
        await run_query(ctx, "SELECT * FROM truth.expected_ledger")


async def test_cost_ceiling_rejects_pathological_query(ctx: ToolContext) -> None:
    from backline.tools.sqlpolicy import SqlPolicyViolation

    with pytest.raises(SqlPolicyViolation, match="cost"):
        # A 468K x 468K self cross-join: the planner prices it in the billions.
        await run_query(
            ctx,
            "SELECT count(*) FROM label.statement_lines a, label.statement_lines b "
            "WHERE a.gross_amount = b.gross_amount",
        )


async def test_sql_errors_propagate_with_message(ctx: ToolContext) -> None:
    with pytest.raises(asyncpg.PostgresError):
        await run_query(ctx, "SELECT no_such_column FROM label.artists")


async def test_empty_result_reports_zero_rows(ctx: ToolContext) -> None:
    out = await run_query(ctx, "SELECT id FROM label.artists WHERE id = -1")
    assert "0 rows" in out
