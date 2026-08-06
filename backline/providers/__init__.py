"""Provider abstraction (BUILD_PLAN §4.1).

Every LLM call in the repo goes through a ``Provider`` implementation in this package —
invariant 6 forbids silent LLM calls anywhere else. The wire types are provider-neutral;
each provider normalizes its native format (Anthropic Messages, OpenAI chat completions)
to and from this shape.
"""

from backline.providers.base import (
    CompletionRequest,
    CompletionResult,
    Message,
    Provider,
    ProviderError,
    StopReason,
    ToolCall,
    ToolSpec,
    Usage,
)
from backline.providers.registry import ModelInfo, ModelRegistry

__all__ = [
    "CompletionRequest",
    "CompletionResult",
    "Message",
    "ModelInfo",
    "ModelRegistry",
    "Provider",
    "ProviderError",
    "StopReason",
    "ToolCall",
    "ToolSpec",
    "Usage",
]
