"""Per-agent ``AgentSpec`` factories (Phase 4): tool sets, model policy, finalizers.

The three agents are the same runtime configured three ways (§2). Tool sets follow
the §4.3 matrix plus the Phase 4 Reconciler additions (scan_anomalies,
compute_allocations — D-013). Model policy comes from settings: ``planner_model``
drives the loop, ``utility_model`` summarizes/compresses. Each spec carries its
prompt file's sha256 in ``trace_attrs`` so every run — and every Phase 5 eval
result — pins to the exact prompt version it saw.

Finalizers turn the model's final text into the typed ``FinalAnswer`` (§4.2):

- citations are extracted structurally (``FBR-C-00501 §3`` patterns, deduped);
- a first line ``ABSTAIN: <reason>`` marks the typed abstention;
- the Reconciler's wrap-up lines ``BATCH:`` / ``FLAGS:`` parse into
  ``ReconcilerAnswer(batch_id, flags_summary)``.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from backline.agents.injection import injection_result_check
from backline.agents.promptfiles import load_prompt
from backline.config import get_settings
from backline.core.guardrails import RunLimits
from backline.core.runtime import AgentSpec, Citation, FinalAnswer, Tool
from backline.tools import (
    ToolContext,
    build_calc_royalties_tool,
    build_compute_allocations_tool,
    build_ingest_statement_tool,
    build_match_lines_tool,
    build_read_clause_tool,
    build_recall_notes_tool,
    build_save_note_tool,
    build_scan_anomalies_tool,
    build_search_contracts_tool,
    build_sql_query_tool,
    build_submit_batch_tool,
    sql_policy_check,
)

AGENT_NAMES = ("counsel", "analyst", "reconciler")

_CITATION = re.compile(r"FBR-[CA]-\d{5}\s+§[A-Z0-9]+")
_ABSTAIN = re.compile(r"^\s*ABSTAIN:\s*(?P<reason>.*)$")
_ANSWER = re.compile(r"^\s*ANSWER:\s*(?P<payload>.*)$")
_BATCH = re.compile(r"^\s*BATCH:\s*(?P<batch>none|\d+)\s*$", re.MULTILINE | re.IGNORECASE)
_FLAGS = re.compile(r"^\s*FLAGS:\s*(?P<summary>.+?)\s*$", re.MULTILINE)


class ReconcilerAnswer(FinalAnswer):
    """§4.2's Reconciler termination shape: ``batch_id`` + ``flags_summary``."""

    batch_id: int | None = None
    flags_summary: str = ""


def extract_citations(text: str) -> list[Citation]:
    seen: list[str] = []
    for match in _CITATION.findall(text):
        ref = " ".join(match.split())
        if ref not in seen:
            seen.append(ref)
    return [Citation(ref=ref) for ref in seen]


def _abstained(text: str) -> bool:
    """Typed abstention under the output contracts (D-018).

    The prompts ask for a first-line ``ABSTAIN:``, but output contracts
    simultaneously demand the reply *end* with an ``ANSWER:`` line — and every
    failing abstention in run 2b9f39fb resolved that tension one of two ways:
    the ``ABSTAIN:`` line displaced to just above a final placeholder ``ANSWER:``
    line (``ANSWER: N/A``, ``ANSWER: $0``...), or the abstention jammed into the
    line itself (``ANSWER: ABSTAIN: no such artist``). All of those are the typed
    protocol; an ``ABSTAIN:`` buried mid-reasoning still is not.
    """
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return False
    final_answer = _ANSWER.match(lines[-1])
    if final_answer is not None and final_answer.group("payload").startswith("ABSTAIN"):
        return True
    candidates = [lines[0], lines[-1]]
    if final_answer is not None and len(lines) >= 2:
        candidates.append(lines[-2])
    return any(_ABSTAIN.match(line) is not None for line in candidates)


def finalize_cited(text: str) -> FinalAnswer:
    """Counsel/Analyst: citations from structural patterns, ABSTAIN from the first line."""
    return FinalAnswer(
        answer=text,
        citations=extract_citations(text),
        abstained=_abstained(text),
    )


def finalize_reconciler(text: str) -> ReconcilerAnswer:
    batch_match = _BATCH.search(text)
    batch_id: int | None = None
    if batch_match is not None and batch_match.group("batch").lower() != "none":
        batch_id = int(batch_match.group("batch"))
    flags_match = _FLAGS.search(text)
    return ReconcilerAnswer(
        answer=text,
        citations=extract_citations(text),
        abstained=_abstained(text),
        batch_id=batch_id,
        flags_summary=flags_match.group("summary") if flags_match is not None else "",
    )


_TOOL_SETS: dict[str, tuple[Callable[[ToolContext], Tool[Any]], ...]] = {
    # §4.3 matrix: Counsel is RAG-heavy + calculator + notes.
    "counsel": (
        build_search_contracts_tool,
        build_read_clause_tool,
        build_calc_royalties_tool,
        build_save_note_tool,
        build_recall_notes_tool,
    ),
    # Analyst is SQL-only (+ notes): royalty math is not analytics.
    "analyst": (
        build_sql_query_tool,
        build_save_note_tool,
        build_recall_notes_tool,
    ),
    # Reconciler is the workflow agent: ingestion chain + scan + allocations +
    # the one write path, plus SQL/search for investigation.
    "reconciler": (
        build_sql_query_tool,
        build_search_contracts_tool,
        build_calc_royalties_tool,
        build_ingest_statement_tool,
        build_match_lines_tool,
        build_scan_anomalies_tool,
        build_compute_allocations_tool,
        build_submit_batch_tool,
        build_save_note_tool,
        build_recall_notes_tool,
    ),
}


def _limits_for(name: str) -> RunLimits:
    base = RunLimits.from_settings()
    if name != "reconciler":
        return base
    # The Reconciler is a workflow, not a Q&A turn: ingest+match per drop, scan,
    # allocations, submit. Double the iteration/budget headroom, allow the slow
    # batch-allocation call, and keep multi-row tool reports un-compressed.
    return RunLimits(
        max_iterations=base.max_iterations * 2,
        run_budget_usd=base.run_budget_usd * 2,
        tool_timeout_s=max(base.tool_timeout_s, 120.0),
        max_result_tokens=max(base.max_result_tokens, 4000),
    )


def build_agent(
    name: str,
    ctx: ToolContext,
    *,
    model: str | None = None,
    utility_model: str | None = None,
) -> AgentSpec:
    """Assemble one agent on the shared runtime primitives.

    ``model``/``utility_model`` override the settings policy (benchmarks, tests);
    the default is the configured planner/utility tier.
    """
    if name not in AGENT_NAMES:
        raise ValueError(f"unknown agent {name!r} — one of {', '.join(AGENT_NAMES)}")
    settings = get_settings()
    prompt = load_prompt(name)
    tools = [build(ctx) for build in _TOOL_SETS[name]]
    finalize = finalize_reconciler if name == "reconciler" else finalize_cited
    return AgentSpec(
        name=name,
        system_prompt=prompt.text,
        model=model or settings.planner_model,
        tools=tools,
        utility_model=utility_model or settings.utility_model,
        limits=_limits_for(name),
        checks=(sql_policy_check,),
        result_checks=(injection_result_check,),
        finalize=finalize,
        trace_attrs={"prompt_sha256": prompt.short_hash},
    )
