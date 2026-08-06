"""The front door (§2): cheap-model classify → agent | clarify, with confidence.

One forced tool call on the router tier decides where a message goes; below the
confidence threshold the router asks a clarifying question instead of guessing. The
call is a traced run like any other LLM work (invariant 6): ``agent="router"``, one
``llm_call`` span with tokens/cost, and the decision recorded in run meta. The
router also surfaces the artist names it saw — the entity signal note auto-recall
(§4.5 scope 3) keys off.

Failure posture: the router never breaks the front door on its own judgment —
an unparseable or missing tool call degrades to ``clarify`` with confidence 0.
Provider errors (network, auth) propagate; that is an outage, not a routing call.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from backline.agents.promptfiles import load_prompt
from backline.config import get_settings
from backline.core.costmeter import CostMeter
from backline.core.trace import Tracer
from backline.providers.base import (
    CompletionRequest,
    CompletionResult,
    Message,
    Provider,
    ToolSpec,
)
from backline.providers.registry import ModelRegistry

RouteTarget = Literal["counsel", "analyst", "reconciler", "clarify"]

_DEFAULT_CLARIFY = (
    "I can look up contract terms (Counsel), run catalog/revenue analytics "
    "(Analyst), or process statement drops (Reconciler) — which of these do you "
    "need, and for which artist or period?"
)


class RouteDecision(BaseModel):
    """The router's verdict — also the schema of the forced `route` tool call."""

    model_config = ConfigDict(frozen=True)

    target: RouteTarget = Field(description="which agent should handle the message")
    confidence: float = Field(ge=0.0, le=1.0, description="honest probability 0-1")
    reason: str = Field(default="", description="one sentence naming the deciding signal")
    clarifying_question: str | None = Field(
        default=None,
        description="when target=clarify: the one-line question to ask the user",
    )
    artists: list[str] = Field(
        default_factory=list,
        description="artist names mentioned in the message, verbatim",
    )


_ROUTE_TOOL = ToolSpec(
    name="route",
    description=(
        "Deliver your routing decision. Call exactly once with the chosen target, "
        "your honest confidence, the deciding signal, artist names mentioned, and — "
        "for clarify — the clarifying question."
    ),
    input_schema=RouteDecision.model_json_schema(),
)


def _clarify(reason: str, *, artists: list[str] | None = None) -> RouteDecision:
    return RouteDecision(
        target="clarify",
        confidence=0.0,
        reason=reason,
        clarifying_question=_DEFAULT_CLARIFY,
        artists=artists or [],
    )


def _parse_decision(result: CompletionResult) -> RouteDecision:
    call = next((c for c in result.tool_calls if c.name == "route"), None)
    if call is None:
        return _clarify("router returned no route tool call")
    try:
        return RouteDecision.model_validate(call.arguments)
    except ValidationError as error:
        first = error.errors(include_url=False)[0]
        return _clarify(f"route arguments failed validation: {first['msg']}")


class Router:
    """Assembled like the runtime: providers + registry + tracer, model from policy."""

    def __init__(
        self,
        *,
        providers: Mapping[str, Provider],
        registry: ModelRegistry,
        tracer: Tracer,
        model: str | None = None,
        confidence_threshold: float | None = None,
    ) -> None:
        settings = get_settings()
        self._providers = dict(providers)
        self._registry = registry
        self._tracer = tracer
        self._model = model or settings.router_model
        self._threshold = (
            settings.router_confidence_threshold
            if confidence_threshold is None
            else confidence_threshold
        )

    async def route(self, message: str, *, session_id: UUID | None = None) -> RouteDecision:
        prompt = load_prompt("router")
        info = self._registry.get(self._model)
        provider = self._providers.get(info.provider)
        if provider is None:
            raise RuntimeError(
                f"router model {self._model!r} needs provider {info.provider!r}, "
                f"but only {sorted(self._providers)} are configured"
            )
        costmeter = CostMeter(self._registry)
        async with self._tracer.run(
            agent="router",
            session_id=session_id,
            meta={"model": self._model, "prompt_sha256": prompt.short_hash},
        ) as run:
            async with run.span("llm_call", f"llm:{self._model}") as span:
                result = await provider.complete(
                    CompletionRequest(
                        model=self._model,
                        system=prompt.text,
                        messages=[Message(role="user", content=message)],
                        tools=[_ROUTE_TOOL],
                        tool_choice="route",
                        max_tokens=400,
                    )
                )
                cost = costmeter.add(self._model, result.usage)
                span.attrs.update(
                    {
                        "gen_ai.request.model": self._model,
                        "gen_ai.response.model": result.model,
                        "gen_ai.usage.input_tokens": result.usage.input_tokens,
                        "gen_ai.usage.output_tokens": result.usage.output_tokens,
                        "cost_usd": cost,
                        "latency_ms": result.latency_ms,
                        "stop_reason": result.stop_reason,
                    }
                )
            decision = _parse_decision(result)
            if decision.target != "clarify" and decision.confidence < self._threshold:
                decision = RouteDecision(
                    target="clarify",
                    confidence=decision.confidence,
                    reason=(
                        f"confidence {decision.confidence:.2f} below threshold "
                        f"{self._threshold:.2f} (model suggested {decision.target}: "
                        f"{decision.reason})"
                    ),
                    clarifying_question=decision.clarifying_question or _DEFAULT_CLARIFY,
                    artists=decision.artists,
                )
            if decision.target == "clarify" and not decision.clarifying_question:
                decision = decision.model_copy(update={"clarifying_question": _DEFAULT_CLARIFY})
            run.meta["route_target"] = decision.target
            run.meta["route_confidence"] = decision.confidence
            run.set_result(status="completed", cost_usd=costmeter.total_usd)
        return decision
