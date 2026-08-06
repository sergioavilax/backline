"""Deterministic scripted provider — every unit/integration test runs on this.

Register a sequence of ``MockTurn``s; each ``complete()`` call consumes the next one in
order. A turn may pin a ``match`` substring that must appear somewhere in the rendered
request (system prompt, message contents, tool calls, tool results) — a mismatch is a
loud ``ProviderError``, so a drifting test scenario fails at the exact turn where the
conversation diverged. Zero tests require an API key (invariant 8).
"""

import time

from pydantic import BaseModel, ConfigDict, Field

from backline.providers.base import (
    CompletionRequest,
    CompletionResult,
    ProviderError,
    StopReason,
    ToolCall,
    Usage,
)


class MockTurn(BaseModel):
    """One canned completion. ``stop_reason`` derives from ``tool_calls`` when unset."""

    model_config = ConfigDict(frozen=True)

    text: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    usage: Usage = Usage(input_tokens=120, output_tokens=30)
    stop_reason: StopReason | None = None
    match: str | None = None


def _render(req: CompletionRequest) -> str:
    """Flatten a request to one searchable string for ``match`` checks."""
    parts = [req.system]
    for message in req.messages:
        parts.append(message.content)
        for call in message.tool_calls:
            parts.append(f"{call.name}({call.arguments!r})")
    return "\n".join(p for p in parts if p)


class MockProvider:
    name = "mock"

    def __init__(self, script: list[MockTurn]) -> None:
        self._script = list(script)
        self._cursor = 0
        self.calls: list[CompletionRequest] = []

    async def complete(self, req: CompletionRequest) -> CompletionResult:
        started = time.perf_counter()
        self.calls.append(req)
        if self._cursor >= len(self._script):
            raise ProviderError(
                f"mock script exhausted after {len(self._script)} turn(s)", provider=self.name
            )
        turn = self._script[self._cursor]
        self._cursor += 1
        if turn.match is not None:
            rendered = _render(req)
            if turn.match not in rendered:
                raise ProviderError(
                    f"mock turn {self._cursor} expected {turn.match!r} in the request; "
                    f"rendered request starts: {rendered[:200]!r}",
                    provider=self.name,
                )
        stop: StopReason = turn.stop_reason or ("tool_use" if turn.tool_calls else "end_turn")
        return CompletionResult(
            text=turn.text,
            tool_calls=list(turn.tool_calls),
            usage=turn.usage,
            stop_reason=stop,
            model=req.model,
            latency_ms=int((time.perf_counter() - started) * 1000),
        )
