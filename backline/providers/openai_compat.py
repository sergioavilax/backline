"""OpenAICompatProvider — any OpenAI-format endpoint: vLLM local models, OpenAI itself.

Speaks ``POST {base_url}/chat/completions`` over httpx and normalizes the wire format
to the internal shape: tool-call ``arguments`` arrive as JSON *strings* here (unlike
Anthropic's parsed objects) and are parsed on the way in; ``finish_reason`` maps onto
the internal stop reasons. Retries 429/5xx and transport errors with jittered
exponential backoff. The jitter source is ``secrets`` (not ``random`` — the seeded-RNG
discipline of invariant 4 stays greppable) and is injectable so tests run instantly.
"""

import asyncio
import json
import secrets
import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

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

_FINISH_REASONS: dict[str, StopReason] = {
    "stop": "end_turn",
    "tool_calls": "tool_use",
    "function_call": "tool_use",
    "length": "max_tokens",
    "content_filter": "refusal",
}


def _default_jitter(cap_s: float) -> float:
    return secrets.randbelow(max(1, int(cap_s * 1000))) / 1000


def to_openai_messages(system: str, messages: list[Message]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if system:
        out.append({"role": "system", "content": system})
    for message in messages:
        if message.role == "tool":
            out.append(
                {
                    "role": "tool",
                    "tool_call_id": message.tool_call_id,
                    "content": message.content,
                }
            )
        elif message.role == "assistant":
            entry: dict[str, Any] = {"role": "assistant", "content": message.content or None}
            if message.tool_calls:
                entry["tool_calls"] = [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {
                            "name": call.name,
                            "arguments": json.dumps(call.arguments),
                        },
                    }
                    for call in message.tool_calls
                ]
            out.append(entry)
        else:
            out.append({"role": "user", "content": message.content})
    return out


def _tool_choice(choice: str) -> str | dict[str, Any]:
    if choice == "any":
        return "required"
    if choice in ("auto", "none"):
        return choice
    return {"type": "function", "function": {"name": choice}}


class OpenAICompatProvider:
    name = "openai_compat"

    def __init__(
        self,
        base_url: str | None = None,
        *,
        api_key: str | None = None,
        max_retries: int = 4,
        backoff_base_s: float = 0.5,
        timeout: float = 300.0,
        transport: httpx.AsyncBaseTransport | None = None,
        sleeper: Callable[[float], Awaitable[None]] | None = None,
        jitter: Callable[[float], float] | None = None,
    ) -> None:
        settings = get_settings()
        url = base_url if base_url is not None else settings.openai_compat_base_url
        if not url:
            raise ProviderError(
                "OPENAI_COMPAT_BASE_URL is not set — point it at any OpenAI-format "
                "endpoint (e.g. a local vLLM)",
                provider=self.name,
            )
        key = api_key if api_key is not None else settings.openai_compat_api_key
        headers = {"authorization": f"Bearer {key}"} if key else {}
        self._client = httpx.AsyncClient(
            base_url=url.rstrip("/"), timeout=timeout, transport=transport, headers=headers
        )
        self._max_retries = max_retries
        self._backoff_base_s = backoff_base_s
        self._sleep = sleeper or asyncio.sleep
        self._jitter = jitter or _default_jitter

    async def complete(self, req: CompletionRequest) -> CompletionResult:
        started = time.perf_counter()
        payload: dict[str, Any] = {
            "model": req.model,
            "messages": to_openai_messages(req.system, req.messages),
            "max_tokens": req.max_tokens,
        }
        if req.temperature is not None:
            payload["temperature"] = req.temperature
        if req.tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.input_schema,
                    },
                }
                for t in req.tools
            ]
            payload["tool_choice"] = _tool_choice(req.tool_choice)

        response = await self._post_with_retry(payload)
        data = response.json()
        return self._normalize(data, started)

    async def _post_with_retry(self, payload: dict[str, Any]) -> httpx.Response:
        last_error = ""
        for attempt in range(self._max_retries + 1):
            try:
                response = await self._client.post("/chat/completions", json=payload)
            except httpx.TransportError as exc:
                last_error = f"transport error: {exc}"
            else:
                if response.status_code == 200:
                    return response
                if response.status_code not in (429,) and response.status_code < 500:
                    raise ProviderError(
                        f"openai-compat endpoint returned {response.status_code}: "
                        f"{response.text[:300]}",
                        provider=self.name,
                    )
                last_error = f"status {response.status_code}"
            if attempt < self._max_retries:
                delay = self._backoff_base_s * (2**attempt) + self._jitter(self._backoff_base_s)
                await self._sleep(delay)
        raise ProviderError(
            f"openai-compat request failed after {self._max_retries + 1} attempt(s); "
            f"last: {last_error}",
            provider=self.name,
            retryable=True,
        )

    def _normalize(self, data: dict[str, Any], started: float) -> CompletionResult:
        try:
            choice = data["choices"][0]
            message = choice["message"]
        except (KeyError, IndexError) as exc:
            raise ProviderError(
                f"openai-compat response missing choices/message: {exc!r}", provider=self.name
            ) from exc

        tool_calls: list[ToolCall] = []
        for raw_call in message.get("tool_calls") or []:
            function = raw_call.get("function", {})
            raw_args = function.get("arguments") or "{}"
            try:
                arguments = json.loads(raw_args)
            except json.JSONDecodeError as exc:
                # Local models mangle tool JSON sometimes (BUILD_PLAN §7 Phase 7 risk);
                # surface it as a provider failure rather than guessing at arguments.
                raise ProviderError(
                    f"model returned unparseable tool arguments for "
                    f"{function.get('name')!r}: {raw_args[:200]!r}",
                    provider=self.name,
                ) from exc
            if not isinstance(arguments, dict):
                arguments = {"value": arguments}
            tool_calls.append(
                ToolCall(
                    id=str(raw_call.get("id") or f"call_{len(tool_calls)}"),
                    name=str(function.get("name", "")),
                    arguments=arguments,
                )
            )

        usage = data.get("usage") or {}
        finish = choice.get("finish_reason") or ""
        return CompletionResult(
            text=message.get("content") or "",
            tool_calls=tool_calls,
            usage=Usage(
                input_tokens=int(usage.get("prompt_tokens") or 0),
                output_tokens=int(usage.get("completion_tokens") or 0),
            ),
            stop_reason=_FINISH_REASONS.get(finish, "other"),
            model=str(data.get("model") or ""),
            latency_ms=int((time.perf_counter() - started) * 1000),
        )

    async def aclose(self) -> None:
        await self._client.aclose()
