"""The ambient run id (keyless): set for the duration of a run, reset after.

Tool handlers stamp gated writes with the proposing run; the contextvar is how they
learn it without widening the handler signature — so it must be exactly the traced
run id during tool execution, and absent outside a run.
"""

from uuid import UUID

from pydantic import BaseModel

from backline.core.runcontext import current_run_id
from backline.core.runtime import AgentRuntime, AgentSpec, Tool
from backline.core.trace import InMemorySink, Tracer
from backline.providers.base import ToolCall
from backline.providers.mock import MockProvider, MockTurn
from backline.providers.registry import ModelRegistry


class NoArgs(BaseModel):
    pass


async def test_run_id_is_ambient_during_tools_and_reset_after() -> None:
    seen: list[UUID | None] = []

    async def peek(_: NoArgs) -> str:
        seen.append(current_run_id.get())
        return "ok"

    tool: Tool[NoArgs] = Tool(
        name="peek", description="records the ambient run id", params=NoArgs, handler=peek
    )
    provider = MockProvider(
        [
            MockTurn(tool_calls=[ToolCall(id="c1", name="peek", arguments={})]),
            MockTurn(text="done"),
        ]
    )
    runtime = AgentRuntime(
        providers={"mock": provider}, registry=ModelRegistry.load(), tracer=Tracer([InMemorySink()])
    )
    agent = AgentSpec(name="ctx-test", system_prompt="peek", model="mock-haiku", tools=[tool])

    assert current_run_id.get() is None
    result = await runtime.run(agent, "go")
    assert seen == [result.run_id]  # inside the run: the traced run id
    assert current_run_id.get() is None  # after: reset, never leaks
