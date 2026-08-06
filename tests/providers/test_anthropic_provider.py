"""AnthropicProvider against canned SSE via httpx.MockTransport — zero network.

The scripted stream splits a tool_use ``input_json_delta`` mid-Unicode-escape, so these
tests pin the partial-JSON assembly pitfall from BUILD_PLAN §9 end-to-end through the
SDK, plus the request-shape mapping (merged tool results, tool_choice, omitted params).
"""

import json
from typing import Any

import httpx
import pytest

from backline.providers.anthropic import AnthropicProvider, to_anthropic_messages
from backline.providers.base import (
    CompletionRequest,
    Message,
    ProviderError,
    ToolCall,
    ToolSpec,
)


def _sse(events: list[tuple[str, dict[str, Any]]]) -> bytes:
    return b"".join(
        f"event: {name}\ndata: {json.dumps(data)}\n\n".encode() for name, data in events
    )


_TOOL_USE_STREAM = _sse(
    [
        (
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": "msg_01",
                    "type": "message",
                    "role": "assistant",
                    "model": "claude-sonnet-5",
                    "content": [],
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 210, "output_tokens": 1},
                },
            },
        ),
        (
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
        ),
        (
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "Looking that up."},
            },
        ),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        (
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 1,
                "content_block": {
                    "type": "tool_use",
                    "id": "toolu_01",
                    "name": "lookup_artist",
                    "input": {},
                },
            },
        ),
        # Partial JSON split mid-key and mid-\u escape — the SDK must reassemble.
        (
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 1,
                "delta": {"type": "input_json_delta", "partial_json": '{"stage_na'},
            },
        ),
        (
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 1,
                "delta": {"type": "input_json_delta", "partial_json": 'me": "Nova R\\u00e'},
            },
        ),
        (
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 1,
                "delta": {"type": "input_json_delta", "partial_json": '9yes"}'},
            },
        ),
        ("content_block_stop", {"type": "content_block_stop", "index": 1}),
        (
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": "tool_use", "stop_sequence": None},
                "usage": {"output_tokens": 45},
            },
        ),
        ("message_stop", {"type": "message_stop"}),
    ]
)


def _provider_with(handler: Any) -> AnthropicProvider:
    return AnthropicProvider(
        api_key="test-key",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        max_retries=2,
    )


async def test_streamed_tool_use_is_assembled_and_normalized() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=_TOOL_USE_STREAM
        )

    provider = _provider_with(handler)
    req = CompletionRequest(
        model="claude-sonnet-5",
        system="You are Counsel.",
        messages=[
            Message(role="user", content="What is Nova's rate?"),
            Message(
                role="assistant",
                content="Checking two sources.",
                tool_calls=[
                    ToolCall(id="a", name="search", arguments={"q": "nova"}),
                    ToolCall(id="b", name="search", arguments={"q": "reyes"}),
                ],
            ),
            Message(role="tool", tool_call_id="a", content="clause 3"),
            Message(role="tool", tool_call_id="b", content="not found", is_error=True),
        ],
        tools=[
            ToolSpec(
                name="lookup_artist",
                description="Find an artist",
                input_schema={
                    "type": "object",
                    "properties": {"stage_name": {"type": "string"}},
                },
            )
        ],
        tool_choice="auto",
        max_tokens=512,
    )

    result = await provider.complete(req)

    # ── normalized response ──
    assert result.text == "Looking that up."
    assert result.tool_calls == [
        ToolCall(id="toolu_01", name="lookup_artist", arguments={"stage_name": "Nova Réyes"})
    ]
    assert result.stop_reason == "tool_use"
    assert result.usage.input_tokens == 210
    assert result.usage.output_tokens == 45
    assert result.model == "claude-sonnet-5"

    # ── outbound request shape ──
    request = seen[0]
    assert request.headers["x-api-key"] == "test-key"
    assert request.headers.get("anthropic-version")  # SDK pins the API version
    body = json.loads(request.content)
    assert body["model"] == "claude-sonnet-5"
    assert body["system"] == "You are Counsel."
    assert body["max_tokens"] == 512
    assert "temperature" not in body  # None means omit — newer models reject it
    assert body["tool_choice"] == {"type": "auto"}
    assert body["tools"][0]["name"] == "lookup_artist"
    # Consecutive tool results merge into ONE user turn with tool_result blocks.
    roles = [m["role"] for m in body["messages"]]
    assert roles == ["user", "assistant", "user"]
    tool_results = body["messages"][2]["content"]
    assert [b["type"] for b in tool_results] == ["tool_result", "tool_result"]
    assert tool_results[0] == {"type": "tool_result", "tool_use_id": "a", "content": "clause 3"}
    assert tool_results[1]["is_error"] is True
    # Assistant turn carries text + both tool_use blocks.
    assistant_blocks = body["messages"][1]["content"]
    assert [b["type"] for b in assistant_blocks] == ["text", "tool_use", "tool_use"]


async def test_retries_on_529_then_succeeds() -> None:
    attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        if len(attempts) == 1:
            return httpx.Response(
                529,
                headers={"retry-after": "0"},
                json={"type": "error", "error": {"type": "overloaded_error", "message": "busy"}},
            )
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=_TOOL_USE_STREAM
        )

    provider = _provider_with(handler)
    result = await provider.complete(
        CompletionRequest(model="claude-sonnet-5", messages=[Message(role="user", content="hi")])
    )
    assert len(attempts) == 2
    assert result.stop_reason == "tool_use"


async def test_non_retryable_error_raises_provider_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"type": "error", "error": {"type": "invalid_request_error", "message": "bad"}},
        )

    provider = _provider_with(handler)
    with pytest.raises(ProviderError, match="400") as excinfo:
        await provider.complete(
            CompletionRequest(model="claude-sonnet-5", messages=[Message(role="user", content="x")])
        )
    assert excinfo.value.retryable is False
    assert excinfo.value.provider == "anthropic"


async def test_missing_api_key_fails_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    import backline.config

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    backline.config.get_settings.cache_clear()
    try:
        with pytest.raises(ProviderError, match="ANTHROPIC_API_KEY"):
            AnthropicProvider(api_key="")
    finally:
        backline.config.get_settings.cache_clear()


def test_specific_tool_choice_maps_to_tool_type() -> None:
    from backline.providers.anthropic import _tool_choice

    assert _tool_choice("any") == {"type": "any"}
    assert _tool_choice("none") == {"type": "none"}
    assert _tool_choice("submit_batch") == {"type": "tool", "name": "submit_batch"}


def test_assistant_message_without_content_still_renders_blocks() -> None:
    mapped = to_anthropic_messages(
        [Message(role="assistant", tool_calls=[ToolCall(id="x", name="t", arguments={})])]
    )
    content = mapped[0]["content"]
    assert isinstance(content, list)
    assert [b["type"] for b in content] == ["tool_use"]
