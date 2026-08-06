import pytest

from backline.providers.base import CompletionRequest, Message, ProviderError, ToolCall, Usage
from backline.providers.mock import MockProvider, MockTurn


def _req(*messages: Message, system: str = "") -> CompletionRequest:
    return CompletionRequest(model="mock-sonnet", messages=list(messages), system=system)


async def test_script_is_consumed_in_order_and_calls_are_recorded() -> None:
    provider = MockProvider(
        [
            MockTurn(tool_calls=[ToolCall(id="t1", name="lookup", arguments={"q": "nova"})]),
            MockTurn(text="done", usage=Usage(input_tokens=10, output_tokens=2)),
        ]
    )

    first = await provider.complete(_req(Message(role="user", content="who is nova?")))
    assert first.stop_reason == "tool_use"  # derived from the presence of tool_calls
    assert first.tool_calls[0].name == "lookup"
    assert first.model == "mock-sonnet"

    second = await provider.complete(_req(Message(role="user", content="ok")))
    assert second.stop_reason == "end_turn"
    assert second.text == "done"
    assert second.usage == Usage(input_tokens=10, output_tokens=2)

    assert len(provider.calls) == 2
    assert provider.calls[0].messages[0].content == "who is nova?"


async def test_match_guards_the_rendered_request() -> None:
    provider = MockProvider(
        [MockTurn(text="hi", match="system prompt marker"), MockTurn(text="x", match="absent")]
    )
    ok = await provider.complete(
        _req(Message(role="user", content="hello"), system="the system prompt marker")
    )
    assert ok.text == "hi"

    with pytest.raises(ProviderError, match="expected 'absent'"):
        await provider.complete(_req(Message(role="user", content="hello")))


async def test_match_sees_tool_calls_and_results() -> None:
    provider = MockProvider([MockTurn(text="ok", match="lookup({'q': 'nova'})")])
    assistant = Message(
        role="assistant", tool_calls=[ToolCall(id="t1", name="lookup", arguments={"q": "nova"})]
    )
    result = await provider.complete(_req(assistant))
    assert result.text == "ok"


async def test_exhausted_script_raises() -> None:
    provider = MockProvider([MockTurn(text="only one")])
    await provider.complete(_req(Message(role="user", content="a")))
    with pytest.raises(ProviderError, match="exhausted after 1 turn"):
        await provider.complete(_req(Message(role="user", content="b")))
