"""route_and_run + entity note auto-recall (§4.5 scope 3). Skips without DATABASE_URL.

One MockProvider script serves both traced runs in order: the router's classify
turn, then the routed agent's turns — so the test pins the *whole* message path,
including what the agent's context actually contained.
"""

import uuid

import asyncpg
import pytest

from backline.agents.dispatch import route_and_run
from backline.agents.recall import compose_user_message, recall_block
from backline.config import get_settings
from backline.core.trace import InMemorySink, Tracer
from backline.providers.base import ToolCall
from backline.providers.mock import MockProvider, MockTurn
from backline.providers.registry import ModelRegistry
from backline.rag.embedder import HashingEmbedder
from backline.rag.reranker import LexicalReranker
from backline.tools.context import ToolContext
from tests.conftest import WorldEnv, requires_postgres

pytestmark = requires_postgres


@pytest.fixture
def ctx(pool: asyncpg.Pool, world_env: WorldEnv) -> ToolContext:
    settings = get_settings().model_copy(update={"data_dir": str(world_env.data_dir)})
    return ToolContext(
        pool=pool,
        settings=settings,
        embedder=HashingEmbedder(),
        reranker=LexicalReranker(),
    )


def _route_turn(**arguments: object) -> MockTurn:
    return MockTurn(tool_calls=[ToolCall(id="r1", name="route", arguments=dict(arguments))])


async def test_dispatch_runs_the_routed_agent_with_recalled_notes(
    ctx: ToolContext, pool: asyncpg.Pool
) -> None:
    artist = await pool.fetchrow("SELECT id, stage_name FROM label.artists ORDER BY id LIMIT 1")
    note = f"{artist['stage_name']}'s deal has a tricky carve-out — check §2 first."
    await pool.execute(
        "INSERT INTO app.notes (entity_ref, body) VALUES ($1, $2)",
        f"artist:{artist['id']}",
        note,
    )

    question = f"What does {artist['stage_name']}'s contract say about territories?"
    provider = MockProvider(
        [
            _route_turn(
                target="counsel",
                confidence=0.9,
                reason="contract terms question",
                artists=[artist["stage_name"]],
            ),
            MockTurn(
                text="Their agreement covers the Territory as defined in §2.",
                match="<recalled_notes>",  # the note reached the agent's context
            ),
        ]
    )
    sink = InMemorySink()

    outcome = await route_and_run(
        question,
        ctx=ctx,
        providers={"mock": provider},
        registry=ModelRegistry.load(),
        tracer=Tracer([sink]),
        session_id=uuid.uuid4(),
        agent_model="mock-sonnet",
        router_model="mock-haiku",
    )

    assert outcome.decision.target == "counsel"
    assert outcome.agent == "counsel"
    assert outcome.result is not None and outcome.result.status == "completed"
    assert note in outcome.recalled_notes

    # The agent's user turn = recalled notes block + the original question, verbatim.
    agent_request = provider.calls[1]
    user_turn = agent_request.messages[-1]
    assert user_turn.role == "user"
    assert user_turn.content.startswith("<recalled_notes>")
    assert note in user_turn.content
    assert user_turn.content.endswith(question)

    # Two traced runs: router then counsel, both completed.
    assert [r.agent for r in sink.runs] == ["router", "counsel"]
    assert all(r.status == "completed" for r in sink.runs)


async def test_dispatch_clarify_short_circuits(ctx: ToolContext) -> None:
    provider = MockProvider(
        [
            _route_turn(
                target="clarify",
                confidence=0.2,
                reason="ambiguous",
                clarifying_question="Which artist and which period do you mean?",
            )
        ]
    )
    sink = InMemorySink()

    outcome = await route_and_run(
        "do the thing with the money",
        ctx=ctx,
        providers={"mock": provider},
        registry=ModelRegistry.load(),
        tracer=Tracer([sink]),
        router_model="mock-haiku",
    )

    assert outcome.decision.target == "clarify"
    assert outcome.clarification == "Which artist and which period do you mean?"
    assert outcome.result is None and outcome.agent is None
    assert len(provider.calls) == 1  # no agent run happened
    assert [r.agent for r in sink.runs] == ["router"]


async def test_recall_block_skips_unknown_and_noteless_artists(
    ctx: ToolContext, pool: asyncpg.Pool
) -> None:
    silent = await pool.fetchrow(
        "SELECT id, stage_name FROM label.artists a WHERE NOT EXISTS "
        "(SELECT 1 FROM app.notes n WHERE n.entity_ref = 'artist:' || a.id) "
        "ORDER BY id DESC LIMIT 1"
    )
    block = await recall_block(pool, ["No Such Artist", silent["stage_name"]])
    assert block == ""
    assert compose_user_message("q?", "") == "q?"
    assert compose_user_message("q?", "<recalled_notes>\nx\n</recalled_notes>") == (
        "<recalled_notes>\nx\n</recalled_notes>\n\nq?"
    )
