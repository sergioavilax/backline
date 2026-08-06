"""Agent assembly (keyless): tool sets, model policy, finalizers, trace pinning."""

from typing import cast

import asyncpg
import pytest

from backline.agents.configs import (
    AGENT_NAMES,
    ReconcilerAnswer,
    build_agent,
    extract_citations,
    finalize_cited,
    finalize_reconciler,
)
from backline.agents.injection import injection_result_check
from backline.agents.promptfiles import load_prompt
from backline.config import get_settings
from backline.tools.context import ToolContext
from backline.tools.sqlpolicy import sql_policy_check


def _ctx() -> ToolContext:
    # Building agents wires tool closures; nothing touches the pool until a handler runs.
    return ToolContext(pool=cast(asyncpg.Pool, object()), settings=get_settings())


def _tool_names(name: str) -> set[str]:
    return {tool.name for tool in build_agent(name, _ctx()).tools}


def test_tool_sets_follow_the_matrix() -> None:
    assert _tool_names("counsel") == {
        "search_contracts",
        "read_clause",
        "calc_royalties",
        "save_note",
        "recall_notes",
    }
    assert _tool_names("analyst") == {"sql_query", "save_note", "recall_notes"}
    assert _tool_names("reconciler") == {
        "sql_query",
        "search_contracts",
        "calc_royalties",
        "ingest_statement",
        "match_lines",
        "scan_anomalies",
        "compute_allocations",
        "submit_batch",
        "save_note",
        "recall_notes",
    }


def test_no_agent_has_an_approval_path() -> None:
    """Invariant 5's Phase 4 face: nothing an agent can call approves/promotes."""
    for name in AGENT_NAMES:
        for tool_name in _tool_names(name):
            assert "approve" not in tool_name
            assert "reject" not in tool_name
            assert "promote" not in tool_name


def test_model_policy_and_prompt_pinning() -> None:
    settings = get_settings()
    for name in AGENT_NAMES:
        agent = build_agent(name, _ctx())
        assert agent.model == settings.planner_model
        assert agent.utility_model == settings.utility_model
        assert agent.trace_attrs["prompt_sha256"] == load_prompt(name).short_hash
        assert agent.system_prompt == load_prompt(name).text
        assert sql_policy_check in agent.checks
        assert injection_result_check in agent.result_checks
    override = build_agent("counsel", _ctx(), model="mock-sonnet", utility_model="mock-haiku")
    assert override.model == "mock-sonnet"
    assert override.utility_model == "mock-haiku"


def test_reconciler_gets_workflow_headroom() -> None:
    settings = get_settings()
    counsel = build_agent("counsel", _ctx())
    reconciler = build_agent("reconciler", _ctx())
    assert counsel.limits.max_iterations == settings.max_iterations
    assert reconciler.limits.max_iterations == settings.max_iterations * 2
    assert reconciler.limits.run_budget_usd == settings.run_budget_usd * 2
    assert reconciler.limits.tool_timeout_s >= 120
    assert reconciler.limits.max_result_tokens >= 4000


def test_unknown_agent_is_loud() -> None:
    with pytest.raises(ValueError, match="unknown agent"):
        build_agent("producer", _ctx())


# ── Finalizers (§4.2 typed termination) ──────────────────────────────────────


def test_citations_extracted_structurally() -> None:
    text = (
        "Nova Reyes earns 30% on streaming (FBR-C-00501 §3), amended for sync by "
        "FBR-A-00712 §A1. See FBR-C-00501 §3 for the base rate."
    )
    refs = [c.ref for c in extract_citations(text)]
    assert refs == ["FBR-C-00501 §3", "FBR-A-00712 §A1"]  # deduped, order kept


def test_finalize_cited_abstention() -> None:
    final = finalize_cited("ABSTAIN: no artist named 'Nova Reyez' on the roster.")
    assert final.abstained is True
    assert final.citations == []
    plain = finalize_cited("The rate is 30% (FBR-C-00501 §3).")
    assert plain.abstained is False
    assert [c.ref for c in plain.citations] == ["FBR-C-00501 §3"]


def test_finalize_cited_abstention_positions() -> None:
    """D-018: the typed abstention may open or close the reply — output contracts
    that demand a final ANSWER-shaped line pull models toward closing with it —
    but an ABSTAIN buried mid-reasoning is still not the protocol."""
    closing = finalize_cited(
        "I searched the roster and the catalog; no artist matches.\n\n"
        "ABSTAIN: no artist named 'Vera Nyx' on the roster."
    )
    assert closing.abstained is True

    trailing_blank = finalize_cited("ABSTAIN: unknown clause.\n\n")
    assert trailing_blank.abstained is True

    buried = finalize_cited(
        "If I could not verify this I would reply ABSTAIN: unknown.\n"
        "The rate is 30% (FBR-C-00501 §3).\n"
        "ANSWER: 30%"
    )
    assert buried.abstained is False

    empty = finalize_cited("")
    assert empty.abstained is False


def test_finalize_cited_abstention_run_2b9f39fb_shapes() -> None:
    """The shapes the first live eval actually produced (9/9 failing abstentions):
    the ABSTAIN line displaced to second-to-last by the contract's mandatory final
    ANSWER placeholder line, or jammed into the ANSWER line's payload. Every one
    is a typed abstention; none invented a value."""
    displaced = finalize_cited(
        'No artist named "Vera Nyx" exists on the roster — the search tool '
        "returned no matches.\n\n"
        'ABSTAIN: No artist named "Vera Nyx" found on the roster.\n\n'
        "ANSWER: N/A%"
    )
    assert displaced.abstained is True

    displaced_money = finalize_cited(
        'No results correspond to an artist named "Moss Delaney".\n\n'
        'ABSTAIN: No artist named "Moss Delaney" found on the roster.\n\n'
        "ANSWER: $0"
    )
    assert displaced_money.abstained is True

    displaced_after_sql = finalize_cited(
        'No artist named "Halcyon Drift" exists in the catalog.\n\n'
        "```sql\nSELECT count(*) FROM label.tracks;\n```\n\n"
        'ABSTAIN: No artist named "Halcyon Drift" found in label.artists.\n\n'
        "ANSWER: 0"
    )
    assert displaced_after_sql.abstained is True

    jammed = finalize_cited(
        "FBR-C-00502 has no §9 — the contract only contains §1 through §8.\n\n"
        "ANSWER: ABSTAIN: FBR-C-00502 has no §9 (clauses run §1 through §8)."
    )
    assert jammed.abstained is True

    # A real answer above a final ANSWER line stays a real answer.
    answered = finalize_cited("The rate is 22% under FBR-C-00503 §3.\n\nANSWER: 22%")
    assert answered.abstained is False


def test_finalize_reconciler_parses_wrap_up() -> None:
    final = finalize_reconciler(
        "Reconciled kinetic 2026-07.\nBATCH: 17\nFLAGS: 4 (error: 2, warning: 2)\n"
        "Two duplicates excluded."
    )
    assert isinstance(final, ReconcilerAnswer)
    assert final.batch_id == 17
    assert final.flags_summary == "4 (error: 2, warning: 2)"
    assert final.abstained is False

    none_batch = finalize_reconciler("Nothing to reconcile.\nBATCH: none\nFLAGS: 0")
    assert none_batch.batch_id is None
    assert none_batch.flags_summary == "0"

    missing = finalize_reconciler("I did some reading but submitted nothing.")
    assert missing.batch_id is None
    assert missing.flags_summary == ""
