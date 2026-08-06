"""OpenAICompatProvider against httpx.MockTransport — zero network, zero real sleeps."""

import json
from typing import Any

import httpx
import pytest

from backline.providers.base import (
    CompletionRequest,
    Message,
    ProviderError,
    ToolCall,
    ToolSpec,
)
from backline.providers.openai_compat import OpenAICompatProvider


def _provider(handler: Any, **kwargs: Any) -> OpenAICompatProvider:
    async def instant_sleep(_: float) -> None:
        return None

    kwargs.setdefault("max_retries", 2)
    return OpenAICompatProvider(
        "http://vllm.local/v1",
        transport=httpx.MockTransport(handler),
        sleeper=instant_sleep,
        jitter=lambda _cap: 0.0,
        **kwargs,
    )


def _completion_body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "id": "cmpl-1",
        "model": "qwen3-8b",
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": "hello"},
            }
        ],
        "usage": {"prompt_tokens": 40, "completion_tokens": 12},
    }
    body.update(overrides)
    return body


async def test_request_shape_and_text_normalization() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_completion_body())

    provider = _provider(handler)
    result = await provider.complete(
        CompletionRequest(
            model="local-qwen",
            system="Be terse.",
            messages=[
                Message(role="user", content="hi"),
                Message(
                    role="assistant",
                    content="",
                    tool_calls=[ToolCall(id="c1", name="probe", arguments={"n": 1})],
                ),
                Message(role="tool", tool_call_id="c1", content="42"),
            ],
            tools=[ToolSpec(name="probe", description="probe it", input_schema={"type": "object"})],
            tool_choice="any",
            max_tokens=128,
            temperature=0.2,
        )
    )

    assert result.text == "hello"
    assert result.stop_reason == "end_turn"
    assert result.usage.input_tokens == 40
    assert result.usage.output_tokens == 12
    assert result.model == "qwen3-8b"

    request = seen[0]
    assert request.url.path == "/v1/chat/completions"
    body = json.loads(request.content)
    assert body["model"] == "local-qwen"
    assert body["max_tokens"] == 128
    assert body["temperature"] == 0.2
    assert body["tool_choice"] == "required"  # internal "any" → OpenAI "required"
    assert body["tools"][0]["function"]["name"] == "probe"
    assert body["messages"][0] == {"role": "system", "content": "Be terse."}
    # Assistant tool calls serialize arguments as JSON strings on this wire format.
    assistant = body["messages"][2]
    assert assistant["tool_calls"][0]["function"]["arguments"] == '{"n": 1}'
    assert body["messages"][3] == {"role": "tool", "tool_call_id": "c1", "content": "42"}


async def test_tool_call_arguments_json_string_is_parsed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_completion_body(
                choices=[
                    {
                        "index": 0,
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_9",
                                    "type": "function",
                                    "function": {
                                        "name": "sql_query",
                                        "arguments": '{"query": "SELECT 1"}',
                                    },
                                }
                            ],
                        },
                    }
                ]
            ),
        )

    provider = _provider(handler)
    result = await provider.complete(
        CompletionRequest(model="local-qwen", messages=[Message(role="user", content="go")])
    )
    assert result.stop_reason == "tool_use"
    assert result.text == ""
    assert result.tool_calls == [
        ToolCall(id="call_9", name="sql_query", arguments={"query": "SELECT 1"})
    ]


async def test_unparseable_tool_arguments_raise() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_completion_body(
                choices=[
                    {
                        "index": 0,
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {"name": "sql_query", "arguments": '{"query": '},
                                }
                            ],
                        },
                    }
                ]
            ),
        )

    provider = _provider(handler)
    with pytest.raises(ProviderError, match="unparseable tool arguments"):
        await provider.complete(
            CompletionRequest(model="local-qwen", messages=[Message(role="user", content="go")])
        )


async def test_retries_on_429_and_500_then_succeeds() -> None:
    statuses = [429, 500]

    def handler(request: httpx.Request) -> httpx.Response:
        if statuses:
            return httpx.Response(statuses.pop(0), json={"error": "busy"})
        return httpx.Response(200, json=_completion_body())

    provider = _provider(handler)
    result = await provider.complete(
        CompletionRequest(model="local-qwen", messages=[Message(role="user", content="hi")])
    )
    assert result.text == "hello"


async def test_retries_exhausted_raises_retryable_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "down"})

    provider = _provider(handler, max_retries=1)
    with pytest.raises(ProviderError, match="failed after 2 attempt") as excinfo:
        await provider.complete(
            CompletionRequest(model="local-qwen", messages=[Message(role="user", content="hi")])
        )
    assert excinfo.value.retryable is True


async def test_client_error_is_not_retried() -> None:
    attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        return httpx.Response(404, json={"error": "no such model"})

    provider = _provider(handler)
    with pytest.raises(ProviderError, match="404"):
        await provider.complete(
            CompletionRequest(model="local-qwen", messages=[Message(role="user", content="hi")])
        )
    assert len(attempts) == 1


async def test_missing_base_url_fails_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    import backline.config

    monkeypatch.delenv("OPENAI_COMPAT_BASE_URL", raising=False)
    backline.config.get_settings.cache_clear()
    try:
        with pytest.raises(ProviderError, match="OPENAI_COMPAT_BASE_URL"):
            OpenAICompatProvider()
    finally:
        backline.config.get_settings.cache_clear()
