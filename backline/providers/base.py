"""Provider-neutral wire types and the ``Provider`` protocol (BUILD_PLAN §4.1)."""

from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

Role = Literal["user", "assistant", "tool"]

StopReason = Literal["end_turn", "tool_use", "max_tokens", "refusal", "other"]
"""Normalized stop reasons; provider-native values map onto these five."""


class ToolCall(BaseModel):
    """A tool invocation requested by the model, arguments already parsed to a dict."""

    model_config = ConfigDict(frozen=True)

    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class Message(BaseModel):
    """One conversation turn in the internal shape.

    - ``user``: plain text content.
    - ``assistant``: text and/or the tool calls the model made that turn.
    - ``tool``: the result of one tool call; ``tool_call_id`` links it back.
    """

    role: Role
    content: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    tool_call_id: str | None = None
    is_error: bool = False  # tool results only: the call failed / was rejected


class ToolSpec(BaseModel):
    """A self-describing tool exposed to the model (JSON schema for its arguments)."""

    model_config = ConfigDict(frozen=True)

    name: str
    description: str
    input_schema: dict[str, Any]


class Usage(BaseModel):
    model_config = ConfigDict(frozen=True)

    input_tokens: int = 0
    output_tokens: int = 0


class CompletionRequest(BaseModel):
    """One model call. ``tool_choice`` is ``auto`` | ``any`` | ``none`` | a tool name."""

    model: str
    messages: list[Message]
    system: str = ""
    tools: list[ToolSpec] = Field(default_factory=list)
    tool_choice: str = "auto"
    max_tokens: int = 4096
    temperature: float | None = None  # None = provider default (newer models reject explicit)


class CompletionResult(BaseModel):
    text: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    usage: Usage = Usage()
    stop_reason: StopReason = "end_turn"
    model: str = ""  # the model that actually served the request
    latency_ms: int = 0


@runtime_checkable
class Provider(Protocol):
    """The one gate for LLM calls (invariant 6: no silent LLM calls anywhere)."""

    name: str

    async def complete(self, req: CompletionRequest) -> CompletionResult: ...


class ProviderError(RuntimeError):
    """A completion failed after the provider's own retries were exhausted."""

    def __init__(self, message: str, *, provider: str, retryable: bool = False) -> None:
        super().__init__(message)
        self.provider = provider
        self.retryable = retryable
