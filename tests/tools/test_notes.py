"""save_note / recall_notes — durable entity-keyed memory (skips without DATABASE_URL)."""

import uuid

import asyncpg
import pytest

from backline.config import get_settings
from backline.core.runcontext import current_run_id
from backline.tools.context import ToolContext
from backline.tools.notes import build_recall_notes_tool, build_save_note_tool
from tests.conftest import requires_postgres

pytestmark = requires_postgres


@pytest.fixture
def ctx(pool: asyncpg.Pool) -> ToolContext:
    return ToolContext(pool=pool, settings=get_settings())


async def test_save_then_recall_round_trip(ctx: ToolContext) -> None:
    save = build_save_note_tool(ctx)
    recall = build_recall_notes_tool(ctx)
    ref = f"artist:{uuid.uuid4().hex[:8]}"  # unique ref keeps reruns isolated
    text = "JP carve-out trips people up — royalties never accrue there."
    saved = await save.handler(save.params(entity_ref=ref, text=text))
    assert ref in saved
    recalled = await recall.handler(recall.params(entity_ref=ref))
    assert text in recalled


async def test_recall_empty_says_so(ctx: ToolContext) -> None:
    recall = build_recall_notes_tool(ctx)
    out = await recall.handler(recall.params(entity_ref="artist:never-noted"))
    assert "no notes" in out.lower()


async def test_notes_stamp_the_proposing_run(ctx: ToolContext, pool: asyncpg.Pool) -> None:
    run_id = uuid.uuid4()
    await pool.execute(
        "INSERT INTO app.runs (id, agent, status, started_at) "
        "VALUES ($1, 'test', 'running', now())",
        run_id,
    )
    ref = f"contract:{uuid.uuid4().hex[:8]}"
    save = build_save_note_tool(ctx)
    token = current_run_id.set(run_id)
    try:
        await save.handler(save.params(entity_ref=ref, text="stamped"))
    finally:
        current_run_id.reset(token)
    created_by = await pool.fetchval("SELECT created_by FROM app.notes WHERE entity_ref = $1", ref)
    assert created_by == run_id


async def test_entity_ref_shape_is_validated(ctx: ToolContext) -> None:
    save = build_save_note_tool(ctx)
    with pytest.raises(ValueError):
        save.params(entity_ref="not a ref", text="x")
    with pytest.raises(ValueError):
        save.params(entity_ref="artist:7", text="   ")


async def test_recall_returns_newest_first_capped(ctx: ToolContext) -> None:
    save = build_save_note_tool(ctx)
    recall = build_recall_notes_tool(ctx)
    ref = f"artist:{uuid.uuid4().hex[:8]}"
    for i in range(25):
        await save.handler(save.params(entity_ref=ref, text=f"note number {i}"))
    out = await recall.handler(recall.params(entity_ref=ref))
    assert "note number 24" in out
    assert "note number 0" not in out  # capped at 20, newest first
    assert out.index("note number 24") < out.index("note number 10")
