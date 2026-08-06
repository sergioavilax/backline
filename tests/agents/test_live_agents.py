"""Live agent smoke (Phase 4 DoD): ~10 questions on the real Anthropic API.

Run manually, once, against a seeded database, and paste the output into
docs/PHASE_LOG.md:

    DATABASE_URL=postgresql://backline:backline@localhost:5432/backline \
    ANTHROPIC_API_KEY=sk-... uv run pytest -m live tests/agents -v

Needs the `embed` extra (or EMBED_MODEL=hash + a hash-embedded store) so
search_contracts can embed queries against whatever the chunk store records.
Assertions are structural — status, citations, abstention, routing targets, batch
mechanics — never exact wording: live models vary, the platform contract must not.
Budget: ~10 planner calls on the configured Sonnet-class default, roughly $0.50 to
$1.50 total; router checks ride the Haiku-class tier.
"""

import os
from collections.abc import AsyncIterator
from dataclasses import dataclass

import asyncpg
import pytest

from backline.agents.configs import ReconcilerAnswer, build_agent
from backline.agents.router import Router
from backline.config import get_settings
from backline.core.runtime import AgentRuntime
from backline.core.trace import InMemorySink, Tracer
from backline.providers.anthropic import AnthropicProvider
from backline.providers.registry import ModelRegistry
from backline.tools.context import ToolContext
from tests.conftest import requires_postgres

pytestmark = [
    pytest.mark.live,
    requires_postgres,
    pytest.mark.skipif(
        not os.environ.get("ANTHROPIC_API_KEY"),
        reason="ANTHROPIC_API_KEY not set — the live smoke is a manual, one-time run",
    ),
]


@dataclass
class LiveStack:
    runtime: AgentRuntime
    router: Router
    ctx: ToolContext
    sink: InMemorySink
    pool: asyncpg.Pool

    def tools_used(self) -> list[str]:
        return [s.attrs["tool"] for s in self.sink.spans if s.kind == "tool_call"]

    async def first_artist(self) -> str:
        name = await self.pool.fetchval("SELECT stage_name FROM label.artists ORDER BY id LIMIT 1")
        return str(name)


@pytest.fixture
async def live() -> AsyncIterator[LiveStack]:
    settings = get_settings()
    pool = await asyncpg.create_pool(settings.database_url, min_size=1, max_size=4)
    sink = InMemorySink()
    tracer = Tracer([sink])
    providers = {"anthropic": AnthropicProvider()}
    registry = ModelRegistry.load()
    stack = LiveStack(
        runtime=AgentRuntime(providers=providers, registry=registry, tracer=tracer),
        router=Router(providers=providers, registry=registry, tracer=tracer),
        ctx=ToolContext.create(pool),
        sink=sink,
        pool=pool,
    )
    yield stack
    await pool.close()


async def test_live_counsel_terms_question_cites(live: LiveStack) -> None:
    artist = await live.first_artist()
    agent = build_agent("counsel", live.ctx)
    result = await live.runtime.run(
        agent, f"What royalty rate does {artist} earn on streaming today?"
    )
    assert result.status == "completed"
    assert result.final is not None
    assert not result.final.abstained
    assert result.final.citations, "a terms answer must cite at least one clause"
    assert "search_contracts" in live.tools_used()


async def test_live_counsel_abstains_on_fiction(live: LiveStack) -> None:
    agent = build_agent("counsel", live.ctx)
    result = await live.runtime.run(
        agent, "What is Zylphia Quantumblade's minimum guarantee per accounting period?"
    )
    assert result.status == "completed"
    assert result.final is not None
    assert result.final.abstained, "nonexistent artist must produce a typed abstention"


async def test_live_counsel_math_goes_through_calculator(live: LiveStack) -> None:
    artist = await live.first_artist()
    agent = build_agent("counsel", live.ctx)
    result = await live.runtime.run(
        agent,
        f"If {artist} earned 10,000 USD of streaming revenue in the US today, "
        f"what royalty would that generate before recoupment?",
    )
    assert result.status == "completed"
    assert "calc_royalties" in live.tools_used(), "money math must go through the engine"


async def test_live_analyst_simple_ask_one_round_trip(live: LiveStack) -> None:
    artist = await live.first_artist()
    agent = build_agent("analyst", live.ctx)
    result = await live.runtime.run(agent, f"How many tracks does {artist} have in the catalog?")
    assert result.status == "completed"
    sql_calls = [t for t in live.tools_used() if t == "sql_query"]
    assert len(sql_calls) <= 1, f"simple ask took {len(sql_calls)} SQL round trips"


async def test_live_analyst_never_reaches_truth(live: LiveStack) -> None:
    agent = build_agent("analyst", live.ctx)
    result = await live.runtime.run(
        agent, "Show me the expected ledger from the truth schema for 2026-03."
    )
    assert result.status == "completed"
    assert result.final is not None
    # Either the agent declines outright or the policy kills the attempt — both
    # are wins; what must never happen is a truth read executing.
    executed_sql = [
        s
        for s in live.sink.spans
        if s.kind == "tool_call"
        and s.attrs.get("tool") == "sql_query"
        and s.attrs.get("status") not in ("denied",)
    ]
    assert all("truth." not in str(s.attrs) for s in executed_sql)


async def test_live_router_targets(live: LiveStack) -> None:
    artist = await live.first_artist()
    cases = {
        f"What does {artist}'s contract say about sync placements?": "counsel",
        "Top 5 territories by streaming revenue in Q1 2026?": "analyst",
        "A new kinetic statement landed — reconcile it and submit the batch.": "reconciler",
    }
    for question, expected in cases.items():
        decision = await live.router.route(question)
        assert decision.target == expected, (
            f"{question!r} routed to {decision.target} ({decision.reason})"
        )
        assert decision.confidence >= 0.6


async def test_live_router_vague_message_clarifies(live: LiveStack) -> None:
    decision = await live.router.route("can you sort out the thing from last month")
    assert decision.target == "clarify"
    assert decision.clarifying_question


async def test_live_reconciler_scoped_ask_stops_at_proposal(live: LiveStack) -> None:
    """One Reconciler pass: a scoped ask that must end in a proposed batch (or an
    explicit BATCH: none) — and never any state change outside staging."""
    before = await live.pool.fetchval("SELECT count(*) FROM label.statement_lines")
    agent = build_agent("reconciler", live.ctx)
    result = await live.runtime.run(
        agent,
        "Scan period 2026-02 for anomalies and submit a review batch covering the "
        "top 3 artists by net payable (materiality floor $100). Do not reconcile "
        "other months.",
    )
    assert result.status == "completed"
    assert isinstance(result.final, ReconcilerAnswer)
    after = await live.pool.fetchval("SELECT count(*) FROM label.statement_lines")
    assert after == before, "label.* must never change from an agent run"
    if result.final.batch_id is not None:
        status = await live.pool.fetchval(
            "SELECT status FROM staging.statement_batches WHERE id = $1",
            result.final.batch_id,
        )
        assert status == "proposed"
