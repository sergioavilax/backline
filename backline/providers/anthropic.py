"""AnthropicProvider — native Messages API tool use over the official SDK.

Built on ``anthropic.AsyncAnthropic`` rather than raw HTTP (D-007): the SDK pins
``anthropic-version``, retries 429/529/5xx and connection errors with jittered
exponential backoff (``max_retries``), and — because every request streams — assembles
``input_json_delta`` fragments into parsed tool arguments (BUILD_PLAN §9 pitfall).
Normalization to the internal shape happens here; nothing outside this module touches
the Anthropic wire format.
"""

import time
from typing import Any, cast

import anthropic
import httpx
from anthropic import AsyncAnthropic, omit
from anthropic.types import MessageParam, ToolChoiceParam, ToolParam

from backline.config import get_settings
from backline.providers.base import (
    CompletionRequest,
    CompletionResult,
    Message,
    ProviderError,
    StopReason,
    ToolCall,
    Usage,
)

_STOP_REASONS: dict[str, StopReason] = {
    "end_turn": "end_turn",
    "stop_sequence": "end_turn",
    "tool_use": "tool_use",
    "max_tokens": "max_tokens",
    "refusal": "refusal",
}

_RETRYABLE_STATUSES = {408, 409, 429, 500, 529}


def to_anthropic_messages(messages: list[Message]) -> list[MessageParam]:
    """Map internal messages to Anthropic content blocks.

    Consecutive ``tool`` results merge into a single user turn — parallel tool calls
    must be answered by one user message carrying all ``tool_result`` blocks.
    """
    out: list[dict[str, Any]] = []
    pending_results: list[dict[str, Any]] = []

    def flush_results() -> None:
        if pending_results:
            out.append({"role": "user", "content": list(pending_results)})
            pending_results.clear()

    for message in messages:
        if message.role == "tool":
            block: dict[str, Any] = {
                "type": "tool_result",
                "tool_use_id": message.tool_call_id,
                "content": message.content,
            }
            if message.is_error:
                block["is_error"] = True
            pending_results.append(block)
            continue
        flush_results()
        if message.role == "assistant":
            blocks: list[dict[str, Any]] = []
            if message.content:
                blocks.append({"type": "text", "text": message.content})
            for call in message.tool_calls:
                blocks.append(
                    {"type": "tool_use", "id": call.id, "name": call.name, "input": call.arguments}
                )
            out.append({"role": "assistant", "content": blocks})
        else:
            out.append({"role": "user", "content": message.content})
    flush_results()
    return cast(list[MessageParam], out)


def _tool_choice(choice: str) -> ToolChoiceParam:
    if choice in ("auto", "any", "none"):
        return cast(ToolChoiceParam, {"type": choice})
    return cast(ToolChoiceParam, {"type": "tool", "name": choice})


class AnthropicProvider:
    name = "anthropic"

    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str | None = None,
        max_retries: int = 4,
        timeout: float = 300.0,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        key = api_key if api_key is not None else get_settings().anthropic_api_key
        if not key:
            raise ProviderError(
                "ANTHROPIC_API_KEY is not set — live agent runs and evals need it; "
                "tests use the MockProvider instead",
                provider=self.name,
            )
        self._client = AsyncAnthropic(
            api_key=key,
            base_url=base_url,
            max_retries=max_retries,
            timeout=timeout,
            http_client=http_client,
        )

    async def complete(self, req: CompletionRequest) -> CompletionResult:
        started = time.perf_counter()
        tools = (
            [
                ToolParam(name=t.name, description=t.description, input_schema=t.input_schema)
                for t in req.tools
            ]
            if req.tools
            else omit
        )
        try:
            async with self._client.messages.stream(
                model=req.model,
                max_tokens=req.max_tokens,
                messages=to_anthropic_messages(req.messages),
                system=req.system if req.system else omit,
                tools=tools,
                tool_choice=_tool_choice(req.tool_choice) if req.tools else omit,
                temperature=req.temperature if req.temperature is not None else omit,
            ) as stream:
                message = await stream.get_final_message()
        except anthropic.APIStatusError as exc:
            raise ProviderError(
                f"anthropic API error {exc.status_code}: {exc.message}",
                provider=self.name,
                retryable=exc.status_code in _RETRYABLE_STATUSES,
            ) from exc
        except anthropic.APIConnectionError as exc:
            raise ProviderError(
                f"anthropic connection error: {exc}", provider=self.name, retryable=True
            ) from exc

        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        for block in message.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                arguments = block.input if isinstance(block.input, dict) else {}
                tool_calls.append(ToolCall(id=block.id, name=block.name, arguments=arguments))
            # thinking/other block types pass through silently — forward-compatible

        stop = _STOP_REASONS.get(message.stop_reason or "", "other")
        return CompletionResult(
            text="".join(text_parts),
            tool_calls=tool_calls,
            usage=Usage(
                input_tokens=message.usage.input_tokens,
                output_tokens=message.usage.output_tokens,
            ),
            stop_reason=stop,
            model=message.model,
            latency_ms=int((time.perf_counter() - started) * 1000),
        )

    async def aclose(self) -> None:
        await self._client.close()
