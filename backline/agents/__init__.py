"""The three agents + router (BUILD_PLAN Phase 4, §2).

Counsel, Analyst, and Reconciler are instances of the same ``AgentRuntime``,
differing only in system prompt (versioned files under ``prompts/``, content-hashed
into trace attrs), tool set, and model policy. The Router is the cheap-model front
door: classify → ``{counsel | analyst | reconciler | clarify}`` with confidence.
"""

from backline.agents.configs import AGENT_NAMES, build_agent
from backline.agents.dispatch import DispatchOutcome, route_and_run
from backline.agents.promptfiles import AgentPrompt, load_prompt
from backline.agents.router import RouteDecision, Router

__all__ = [
    "AGENT_NAMES",
    "AgentPrompt",
    "DispatchOutcome",
    "RouteDecision",
    "Router",
    "build_agent",
    "load_prompt",
    "route_and_run",
]
