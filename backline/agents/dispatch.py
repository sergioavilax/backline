"""Route-and-run: the message path a session takes through the platform (Phase 4).

``route_and_run`` is what the CLI harness uses today and the Phase 6 API will wrap:
router classifies (traced run #1); ``clarify`` short-circuits with the question;
otherwise the routed agent runs (traced run #2) with any recalled entity notes
folded into its user turn. Two runs per message is deliberate — the router's
verdict and cost stay separately inspectable in the Trace Inspector.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from uuid import UUID

from backline.agents.configs import build_agent
from backline.agents.recall import compose_user_message, recall_block
from backline.agents.router import RouteDecision, Router
from backline.core.runtime import AgentRuntime, RunResult
from backline.core.trace import Tracer
from backline.providers.base import Provider
from backline.providers.registry import ModelRegistry
from backline.tools.context import ToolContext


@dataclass(frozen=True)
class DispatchOutcome:
    decision: RouteDecision
    agent: str | None  # the agent that ran; None on clarify
    result: RunResult | None  # None on clarify
    recalled_notes: str = ""  # the auto-recall block the agent saw ('' when none)

    @property
    def clarification(self) -> str | None:
        return self.decision.clarifying_question if self.decision.target == "clarify" else None


async def route_and_run(
    message: str,
    *,
    ctx: ToolContext,
    providers: Mapping[str, Provider],
    registry: ModelRegistry,
    tracer: Tracer,
    session_id: UUID | None = None,
    agent_model: str | None = None,
    router_model: str | None = None,
) -> DispatchOutcome:
    """Classify, recall, run. ``agent_model``/``router_model`` override the policy."""
    router = Router(providers=providers, registry=registry, tracer=tracer, model=router_model)
    decision = await router.route(message, session_id=session_id)
    if decision.target == "clarify":
        return DispatchOutcome(decision=decision, agent=None, result=None)

    recalled = await recall_block(ctx.pool, decision.artists)
    agent = build_agent(decision.target, ctx, model=agent_model)
    runtime = AgentRuntime(providers=providers, registry=registry, tracer=tracer)
    result = await runtime.run(
        agent, compose_user_message(message, recalled), session_id=session_id
    )
    return DispatchOutcome(
        decision=decision, agent=decision.target, result=result, recalled_notes=recalled
    )
