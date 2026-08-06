"""Live Anthropic API verification — skipped by default (Phase 2 DoD).

Run manually, once, with a key:

    ANTHROPIC_API_KEY=sk-... uv run pytest -m live -v

Uses ``claude-haiku-4-5``: the live check verifies provider *plumbing* (streaming
assembly, tool-use round trip, usage accounting), not model capability, and BUILD_PLAN
§10 keeps dev pokes cheap — Haiku-class is the plan's designated utility tier.
"""

import os

import pytest

from backline.providers.anthropic import AnthropicProvider
from backline.providers.base import CompletionRequest, Message, ToolSpec

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not os.environ.get("ANTHROPIC_API_KEY"),
        reason="ANTHROPIC_API_KEY not set — live verification is a manual, one-time run",
    ),
]

LIVE_MODEL = "claude-haiku-4-5"


async def test_live_text_completion() -> None:
    provider = AnthropicProvider()
    result = await provider.complete(
        CompletionRequest(
            model=LIVE_MODEL,
            messages=[Message(role="user", content="Reply with exactly the word: backline")],
            max_tokens=32,
        )
    )
    assert "backline" in result.text.lower()
    assert result.stop_reason == "end_turn"
    assert result.usage.input_tokens > 0
    assert result.usage.output_tokens > 0
    assert result.latency_ms > 0


async def test_live_tool_use_round_trip() -> None:
    provider = AnthropicProvider()
    echo_tool = ToolSpec(
        name="echo",
        description="Echo a value back to the caller.",
        input_schema={
            "type": "object",
            "properties": {"value": {"type": "string", "description": "value to echo"}},
            "required": ["value"],
        },
    )
    req = CompletionRequest(
        model=LIVE_MODEL,
        system="Use the echo tool, then report its result verbatim.",
        messages=[Message(role="user", content="Echo the value 'FBR-470'.")],
        tools=[echo_tool],
        tool_choice="echo",  # force the tool so the round trip is deterministic
        max_tokens=256,
    )
    first = await provider.complete(req)
    assert first.stop_reason == "tool_use"
    call = first.tool_calls[0]
    assert call.name == "echo"
    assert "FBR-470" in str(call.arguments.get("value", ""))

    followup = CompletionRequest(
        model=LIVE_MODEL,
        system=req.system,
        messages=[
            *req.messages,
            Message(role="assistant", content=first.text, tool_calls=first.tool_calls),
            Message(role="tool", tool_call_id=call.id, content="FBR-470"),
        ],
        tools=[echo_tool],
        tool_choice="auto",
        max_tokens=256,
    )
    second = await provider.complete(followup)
    assert second.stop_reason == "end_turn"
    assert "FBR-470" in second.text
