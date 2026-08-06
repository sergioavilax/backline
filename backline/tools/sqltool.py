"""The ``sql_query`` tool: read-only analytics over ``label``/``staging`` (§4.3).

Execution order per call: sqlglot policy (parse-level allowlist + LIMIT rewrite, see
``sqlpolicy``) → ``EXPLAIN`` cost ceiling → the query itself, inside a READ ONLY
transaction with a server-side statement timeout (belt and suspenders under the
runtime's own tool timeout). Results render as a compact aligned table + row count,
with the LIMIT rewrite disclosed to the model so a truncated result is never mistaken
for a complete one.
"""

from __future__ import annotations

import json

import asyncpg
from pydantic import BaseModel, ConfigDict, Field

from backline.core.runtime import Tool
from backline.tools.context import ToolContext
from backline.tools.sqlpolicy import SqlPolicyViolation, enforce

_MAX_CELL_WIDTH = 48


class SqlQueryParams(BaseModel):
    model_config = ConfigDict(frozen=True)

    query: str = Field(
        description=(
            "One PostgreSQL SELECT statement (CTEs allowed). Readable schemas: label "
            "(catalog, contracts, statements, fx) and staging (proposed batches). "
            "Schema-qualify every table. LIMIT 200 is enforced."
        )
    )


def _cell(value: object) -> str:
    if value is None:
        return ""
    text = str(value)
    if len(text) > _MAX_CELL_WIDTH:
        text = text[: _MAX_CELL_WIDTH - 1] + "…"
    return text


def format_table(columns: list[str], rows: list[tuple[object, ...]], notes: list[str]) -> str:
    """Aligned pipe-separated table; Decimals arrive as ``Decimal`` and print exactly."""
    lines: list[str] = []
    if rows:
        rendered = [[_cell(v) for v in row] for row in rows]
        widths = [max(len(col), *(len(r[i]) for r in rendered)) for i, col in enumerate(columns)]
        lines.append(" | ".join(col.ljust(w) for col, w in zip(columns, widths, strict=True)))
        lines.append("-+-".join("-" * w for w in widths))
        lines.extend(
            " | ".join(cell.ljust(w) for cell, w in zip(row, widths, strict=True))
            for row in rendered
        )
    count = f"{len(rows)} row" + ("" if len(rows) == 1 else "s")
    tail = " · ".join([count, *notes])
    lines.append(f"({tail})")
    return "\n".join(lines)


async def _plan_cost(conn: asyncpg.Connection, sql: str) -> float:
    plan_json = await conn.fetchval(f"EXPLAIN (FORMAT JSON) {sql}")
    plan = json.loads(plan_json)
    cost = plan[0]["Plan"]["Total Cost"]
    return float(cost)


def build_sql_query_tool(ctx: ToolContext) -> Tool[SqlQueryParams]:
    settings = ctx.settings

    async def handler(params: SqlQueryParams) -> str:
        vetted = enforce(params.query, row_limit=settings.sql_row_limit)
        timeout_ms = max(int(settings.tool_timeout_s * 1000) - 500, 1000)
        async with ctx.pool.acquire() as conn, conn.transaction(readonly=True):
            await conn.execute(f"SET LOCAL statement_timeout = {timeout_ms}")
            cost = await _plan_cost(conn, vetted.sql)
            if cost > settings.sql_cost_ceiling:
                raise SqlPolicyViolation(
                    f"estimated plan cost {cost:,.0f} exceeds the ceiling "
                    f"{settings.sql_cost_ceiling:,.0f} — narrow the query "
                    f"(filter on indexed columns like period/isrc, or pre-aggregate)"
                )
            records = await conn.fetch(vetted.sql)

        notes: list[str] = []
        if vetted.limit_injected:
            notes.append(f"LIMIT {settings.sql_row_limit} applied automatically")
        if vetted.limit_capped:
            notes.append(f"LIMIT capped at {settings.sql_row_limit}")
        columns = list(records[0].keys()) if records else []
        rows = [tuple(record) for record in records]
        return format_table(columns, rows, notes)

    return Tool(
        name="sql_query",
        description=(
            "Run one read-only SQL SELECT against the label database. Readable schemas: "
            "label (artists, releases, tracks, release_tracks, contracts, contract_terms, "
            "amendments, advances, expenses, recoup_accounts, distributors, statements, "
            "statement_lines, fx_rates, dashboard_streams) and staging (statement_batches, "
            "proposed_allocations, flags, ingested_lines). Tables must be schema-qualified. "
            "A LIMIT of 200 rows is enforced; expensive full-table joins are rejected. "
            "Returns an aligned text table plus a row count."
        ),
        params=SqlQueryParams,
        handler=handler,
    )
