import asyncio
from collections.abc import Sequence

from backline.core.memory import SessionMemory, WorkingMemory
from backline.providers.base import Message


def _msg(i: int) -> Message:
    return Message(role="user" if i % 2 == 0 else "assistant", content=f"turn {i}")


async def test_session_memory_returns_everything_under_the_window() -> None:
    memory = SessionMemory(window=4)
    for i in range(3):
        memory.add(_msg(i))
    context = await memory.context()
    assert [m.content for m in context] == ["turn 0", "turn 1", "turn 2"]


async def test_session_memory_folds_overflow_through_the_summarizer() -> None:
    seen: list[list[str]] = []

    async def summarizer(messages: Sequence[Message]) -> str:
        seen.append([m.content for m in messages])
        return "user asked about turns 0-1"

    memory = SessionMemory(window=4, summarizer=summarizer)
    for i in range(6):
        memory.add(_msg(i))

    context = await memory.context()
    # Oldest two folded into a summary; the last four survive verbatim.
    assert seen == [[f"turn {i}" for i in range(2)]]
    assert context[0].role == "user"
    assert "user asked about turns 0-1" in context[0].content
    assert "<conversation_summary>" in context[0].content
    assert [m.content for m in context[1:]] == [f"turn {i}" for i in range(2, 6)]


async def test_session_memory_summary_accumulates_across_compactions() -> None:
    async def summarizer(messages: Sequence[Message]) -> str:
        return f"folded {len(messages)} message(s)"

    memory = SessionMemory(window=2, summarizer=summarizer)
    for i in range(4):
        memory.add(_msg(i))
    first = await memory.context()
    assert "folded 2 message(s)" in first[0].content

    # The next compaction sees the prior summary as part of what it folds.
    for i in range(4, 7):
        memory.add(_msg(i))
    second = await memory.context()
    assert "folded 4 message(s)" in second[0].content  # prior summary + 3 new overflow
    assert [m.content for m in second[1:]] == ["turn 5", "turn 6"]


async def test_session_memory_without_summarizer_elides_deterministically() -> None:
    memory = SessionMemory(window=2)
    for i in range(5):
        memory.add(_msg(i))
    context = await memory.context()
    assert "3 earlier message(s) elided" in context[0].content
    assert [m.content for m in context[1:]] == ["turn 3", "turn 4"]


def test_working_memory_dedups_identical_results_by_hash() -> None:
    working = WorkingMemory()
    first = working.record("lookup_artist", "Nova Reyes, ID 42")
    assert first.deduped is False
    assert first.content == "Nova Reyes, ID 42"

    second = working.record("lookup_artist", "Nova Reyes, ID 42")
    assert second.deduped is True
    assert "duplicate of lookup_artist result #1" in second.content
    assert "Nova Reyes" not in second.content  # the payload itself is elided

    third = working.record("lookup_artist", "Kaiya Marsh, ID 7")
    assert third.deduped is False


def test_working_memory_key_includes_the_tool_name() -> None:
    working = WorkingMemory()
    working.record("sql_query", "42")
    other_tool = working.record("calc_royalties", "42")
    # Same bytes from a different tool are a coincidence, not a duplicate.
    assert other_tool.deduped is False


def test_working_memory_records_entries_for_inspection() -> None:
    working = WorkingMemory()
    working.record("a", "x")
    working.record("a", "x")
    working.record("b", "y")
    assert [e.tool for e in working.entries] == ["a", "b"]  # dedup hits don't re-append


def test_note_elided_reports_pre_window_history() -> None:
    """Phase 6: the API loads a SQL-bounded window and reports what it dropped."""
    memory = SessionMemory(window=5)
    memory.note_elided(12)
    memory.add(Message(role="user", content="latest question"))
    context = asyncio.run(memory.context())
    assert len(context) == 2
    assert "12 earlier message(s) elided" in context[0].content
    assert context[1].content == "latest question"

    try:
        memory.note_elided(-1)
    except ValueError:
        pass
    else:  # pragma: no cover - defensive
        raise AssertionError("negative elision count must raise")
