"""The agent tool registry (BUILD_PLAN §4.3) — every tool an agent can call.

Per-agent tool *sets* are assembled in Phase 4; this package owns the tools
themselves plus ``build_all_tools`` for harnesses that want the full kit.
"""

from typing import Any

from backline.core.runtime import Tool
from backline.tools.calc import build_calc_royalties_tool
from backline.tools.context import ToolContext
from backline.tools.notes import build_recall_notes_tool, build_save_note_tool
from backline.tools.retrieval import build_read_clause_tool, build_search_contracts_tool
from backline.tools.sqlpolicy import sql_policy_check
from backline.tools.sqltool import build_sql_query_tool
from backline.tools.statements import (
    build_ingest_statement_tool,
    build_match_lines_tool,
    build_submit_batch_tool,
)

__all__ = [
    "ToolContext",
    "build_all_tools",
    "build_calc_royalties_tool",
    "build_ingest_statement_tool",
    "build_match_lines_tool",
    "build_read_clause_tool",
    "build_recall_notes_tool",
    "build_save_note_tool",
    "build_search_contracts_tool",
    "build_sql_query_tool",
    "build_submit_batch_tool",
    "sql_policy_check",
]


def build_all_tools(ctx: ToolContext) -> list[Tool[Any]]:
    """Every §4.3 tool, one binding each (agents get subsets of this in Phase 4)."""
    return [
        build_sql_query_tool(ctx),
        build_search_contracts_tool(ctx),
        build_read_clause_tool(ctx),
        build_calc_royalties_tool(ctx),
        build_ingest_statement_tool(ctx),
        build_match_lines_tool(ctx),
        build_submit_batch_tool(ctx),
        build_save_note_tool(ctx),
        build_recall_notes_tool(ctx),
    ]
