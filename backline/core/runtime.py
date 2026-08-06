"""AgentRuntime — the loop every agent runs on (BUILD_PLAN §4.2).

One iteration = assemble context → ``provider.complete`` → either execute the model's
tool calls (validated by guardrails, deduped by working memory, oversize results
compressed by the utility model) or finalize into a typed ``FinalAnswer``.

Hard limits come from ``RunLimits``: a run that trips the iteration or budget cap ends
``status=exhausted`` — never a silent truncation. Every step is traced (run → iteration
→ llm_call/tool_call/guardrail/compression) and every LLM call is metered; providers
are only ever invoked from inside a traced span here (invariant 6).
"""

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from backline.core.costmeter import CostMeter
from backline.core.guardrails import Guardrails, Incident, RunLimits, ToolCheck
from backline.core.memory import SessionMemory, WorkingMemory
from backline.core.trace import RunHandle, SpanHandle, Tracer
from backline.providers.base import (
    CompletionRequest,
    CompletionResult,
    Message,
    Provider,
    ProviderError,
    ToolCall,
    ToolSpec,
)
from backline.providers.registry import ModelRegistry

RunStatus = Literal["completed", "exhausted", "error"]


def estimate_tokens(text: str) -> int:
    """Cheap deterministic token estimate (~chars/4) for result-size limits.

    Exact counts don't matter here — this guards context bloat, and the same
    bytes/4 convention backs datagen's offline corpus estimate.
    """
    return len(text) // 4


@dataclass(frozen=True)
class Tool[P: BaseModel]:
    """A runtime tool binding: Pydantic-typed args, async handler returning text.

    Phase 3 registers the real tools (sql_query, search_contracts, ...); the runtime
    only ever sees this shape.
    """

    name: str
    description: str
    params: type[P]
    handler: Callable[[P], Awaitable[str]]

    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=self.name,
            description=self.description,
            input_schema=self.params.model_json_schema(),
        )


class Citation(BaseModel):
    """A structural source reference; Phase 3 retrieval fills these with clause ids."""

    model_config = ConfigDict(frozen=True)

    ref: str
    note: str = ""


class FinalAnswer(BaseModel):
    """Typed termination contract (§4.2): Counsel/Analyst shape; Phase 4 extends."""

    model_config = ConfigDict(frozen=True)

    answer: str
    citations: list[Citation] = Field(default_factory=list)
    abstained: bool = False


Finalizer = Callable[[str], FinalAnswer]


def default_finalize(text: str) -> FinalAnswer:
    return FinalAnswer(answer=text)


@dataclass(frozen=True)
class AgentSpec:
    """Everything that distinguishes one agent from another (§2: same runtime,
    different system prompt, tool set, and model policy)."""

    name: str
    system_prompt: str
    model: str
    tools: Sequence[Tool[Any]] = ()
    utility_model: str | None = None  # summarization / compression model (Haiku-class)
    limits: RunLimits = field(default_factory=RunLimits)
    checks: Sequence[ToolCheck] = ()
    finalize: Finalizer = default_finalize
    tool_choice: str = "auto"
    max_tokens: int = 4096


class RunResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    run_id: UUID
    status: RunStatus
    final: FinalAnswer | None
    iterations: int
    cost_usd: Decimal


_COMPRESS_SYSTEM = (
    "You summarize oversized tool output for an agent's working context. Preserve "
    "every identifier, number, and monetary amount exactly; drop repetition and "
    "boilerplate. Be faithful — never invent content."
)


class AgentRuntime:
    def __init__(
        self,
        *,
        providers: Mapping[str, Provider],
        registry: ModelRegistry,
        tracer: Tracer,
    ) -> None:
        self._providers = dict(providers)
        self._registry = registry
        self._tracer = tracer

    def _provider_for(self, model_id: str) -> Provider:
        info = self._registry.get(model_id)
        provider = self._providers.get(info.provider)
        if provider is None:
            raise RuntimeError(
                f"model {model_id!r} needs provider {info.provider!r}, but the runtime "
                f"only has: {sorted(self._providers)}"
            )
        return provider

    async def run(
        self,
        agent: AgentSpec,
        user_message: str,
        *,
        session_id: UUID | None = None,
        session: SessionMemory | None = None,
    ) -> RunResult:
        provider = self._provider_for(agent.model)
        guardrails = Guardrails(agent.limits, agent.checks)
        costmeter = CostMeter(self._registry)
        working = WorkingMemory()
        tool_map = {tool.name: tool for tool in agent.tools}
        specs = [tool.spec() for tool in agent.tools]

        messages: list[Message] = []
        if session is not None:
            messages.extend(await session.context())
        messages.append(Message(role="user", content=user_message))

        status: RunStatus = "exhausted"
        final: FinalAnswer | None = None
        iterations = 0

        async with self._tracer.run(
            agent=agent.name, session_id=session_id, meta={"model": agent.model}
        ) as run:
            try:
                for iteration in range(1, agent.limits.max_iterations + 1):
                    budget_incident = guardrails.check_budget(costmeter.total_usd)
                    if budget_incident is not None:
                        await self._record_incident(run, budget_incident)
                        status = "exhausted"
                        break
                    iterations = iteration
                    async with run.span("iteration", f"iteration:{iteration}") as it_span:
                        result = await self._llm_call(
                            it_span, provider, agent, messages, specs, costmeter
                        )
                        if result.tool_calls:
                            messages.append(
                                Message(
                                    role="assistant",
                                    content=result.text,
                                    tool_calls=result.tool_calls,
                                )
                            )
                            for call in result.tool_calls:
                                messages.append(
                                    await self._run_tool(
                                        it_span,
                                        agent,
                                        tool_map,
                                        guardrails,
                                        working,
                                        call,
                                        costmeter,
                                    )
                                )
                        else:
                            final = agent.finalize(result.text)
                            status = "completed"
                            break
                else:
                    cap = guardrails.check_iteration(agent.limits.max_iterations + 1)
                    assert cap is not None  # the loop ran out — the cap is tripped
                    await self._record_incident(run, cap)
                    status = "exhausted"
            except ProviderError as exc:
                status = "error"
                run.meta.setdefault("error", str(exc))
            run.set_result(status=status, cost_usd=costmeter.total_usd)
            run_id = run.run_id

        return RunResult(
            run_id=run_id,
            status=status,
            final=final,
            iterations=iterations,
            cost_usd=costmeter.total_usd,
        )

    async def _llm_call(
        self,
        parent: SpanHandle,
        provider: Provider,
        agent: AgentSpec,
        messages: Sequence[Message],
        specs: Sequence[ToolSpec],
        costmeter: CostMeter,
    ) -> CompletionResult:
        async with parent.span("llm_call", f"llm:{agent.model}") as span:
            started = time.perf_counter()
            result = await provider.complete(
                CompletionRequest(
                    model=agent.model,
                    messages=list(messages),
                    system=agent.system_prompt,
                    tools=list(specs),
                    tool_choice=agent.tool_choice,
                    max_tokens=agent.max_tokens,
                )
            )
            cost = costmeter.add(agent.model, result.usage)
            span.attrs.update(
                {
                    "gen_ai.request.model": agent.model,
                    "gen_ai.response.model": result.model,
                    "gen_ai.usage.input_tokens": result.usage.input_tokens,
                    "gen_ai.usage.output_tokens": result.usage.output_tokens,
                    "cost_usd": cost,
                    "latency_ms": result.latency_ms or int((time.perf_counter() - started) * 1000),
                    "stop_reason": result.stop_reason,
                }
            )
            return result

    async def _run_tool(
        self,
        parent: SpanHandle,
        agent: AgentSpec,
        tool_map: Mapping[str, Tool[Any]],
        guardrails: Guardrails,
        working: WorkingMemory,
        call: ToolCall,
        costmeter: CostMeter,
    ) -> Message:
        async with parent.span("tool_call", f"tool:{call.name}") as span:
            span.attrs["tool"] = call.name
            tool = tool_map.get(call.name)
            validated, incident = guardrails.validate_tool_call(
                call.name, tool.params if tool is not None else None, call.arguments
            )
            if incident is not None or tool is None or validated is None:
                assert incident is not None
                await self._record_incident(parent, incident)
                span.attrs["status"] = "denied"
                return Message(
                    role="tool",
                    tool_call_id=call.id,
                    content=f"tool call rejected by guardrails ({incident.kind}): "
                    f"{incident.detail}",
                    is_error=True,
                )

            started = time.perf_counter()
            try:
                async with asyncio.timeout(agent.limits.tool_timeout_s):
                    raw = await tool.handler(validated)
            except TimeoutError:
                span.attrs["status"] = "timeout"
                return Message(
                    role="tool",
                    tool_call_id=call.id,
                    content=f"tool {call.name} timed out after {agent.limits.tool_timeout_s}s",
                    is_error=True,
                )
            except Exception as exc:  # tool failures go back to the model, traced
                span.attrs["status"] = "error"
                span.attrs["error"] = f"{type(exc).__name__}: {exc}"
                return Message(
                    role="tool",
                    tool_call_id=call.id,
                    content=f"tool {call.name} failed: {exc}",
                    is_error=True,
                )
            span.attrs["latency_ms"] = int((time.perf_counter() - started) * 1000)

            worked = working.record(call.name, raw)
            if worked.deduped:
                span.attrs["deduped"] = True
                return Message(role="tool", tool_call_id=call.id, content=worked.content)

            content = raw
            estimated = estimate_tokens(raw)
            span.attrs["result_tokens_est"] = estimated
            if estimated > agent.limits.max_result_tokens:
                content = await self._compress(parent, agent, raw, estimated, costmeter)
            return Message(role="tool", tool_call_id=call.id, content=content)

    async def _compress(
        self,
        parent: SpanHandle,
        agent: AgentSpec,
        raw: str,
        estimated_tokens: int,
        costmeter: CostMeter,
    ) -> str:
        """Shrink an oversize tool result — utility model if configured, else truncate.

        Either way the compression is a span, so shrunken context is visible in the
        trace instead of being a silent lie about what the model saw.
        """
        limit = agent.limits.max_result_tokens
        async with parent.span(
            "compression", f"compress:{agent.utility_model or 'truncate'}"
        ) as span:
            span.attrs["original_tokens_est"] = estimated_tokens
            span.attrs["max_result_tokens"] = limit
            if agent.utility_model is None:
                span.attrs["method"] = "truncate"
                kept = raw[: limit * 4]
                return (
                    f"[tool result truncated: ~{estimated_tokens} tokens exceeded "
                    f"max_result_tokens={limit}; the first ~{limit} tokens follow]\n{kept}"
                )
            utility = self._provider_for(agent.utility_model)
            result = await utility.complete(
                CompletionRequest(
                    model=agent.utility_model,
                    system=_COMPRESS_SYSTEM,
                    messages=[
                        Message(
                            role="user",
                            content=f"Summarize this tool output in at most ~{limit} "
                            f"tokens:\n\n{raw}",
                        )
                    ],
                    max_tokens=min(limit * 2, 4096),
                )
            )
            cost = costmeter.add(agent.utility_model, result.usage)
            span.attrs.update(
                {
                    "method": "utility_model",
                    "gen_ai.request.model": agent.utility_model,
                    "gen_ai.usage.input_tokens": result.usage.input_tokens,
                    "gen_ai.usage.output_tokens": result.usage.output_tokens,
                    "cost_usd": cost,
                }
            )
            return (
                f"[tool result compressed from ~{estimated_tokens} tokens by "
                f"{agent.utility_model}]\n{result.text}"
            )

    async def _record_incident(self, parent: RunHandle | SpanHandle, incident: Incident) -> None:
        """Guardrail incidents are spans — visible in the Trace Inspector (§4.6)."""
        async with parent.span("guardrail", f"guardrail:{incident.kind}") as span:
            span.attrs["kind"] = incident.kind
            span.attrs["detail"] = incident.detail
            span.attrs["status"] = "incident"
            if incident.tool is not None:
                span.attrs["tool"] = incident.tool
