"""Manual poking harness (Phase 4): one question through the real platform.

    uv run python scripts/ask.py "What is Nova Reyes' sync rate?"          # routed
    uv run python scripts/ask.py --agent counsel "..."                     # direct
    uv run python scripts/ask.py --agent analyst --model claude-haiku-4-5 "..."

Needs a seeded database (`make seed`) and ANTHROPIC_API_KEY (or an OpenAI-compat
endpoint plus --model local-qwen). Every run traces to Postgres + JSONL exactly like
a production run; the span summary prints at the end. This is a dev harness — the
product surface (sessions, SSE streaming) is the Phase 6 API/UI.
"""

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any

import asyncpg

from backline.agents import build_agent, route_and_run
from backline.config import get_settings
from backline.core.runtime import AgentRuntime, RunResult
from backline.core.trace import JsonlSink, PostgresSink, Tracer, TraceSink
from backline.providers.anthropic import AnthropicProvider
from backline.providers.base import Provider
from backline.providers.openai_compat import OpenAICompatProvider
from backline.providers.registry import ModelRegistry
from backline.tools.context import ToolContext


def _providers() -> dict[str, Provider]:
    settings = get_settings()
    providers: dict[str, Provider] = {}
    if settings.anthropic_api_key:
        providers["anthropic"] = AnthropicProvider(api_key=settings.anthropic_api_key)
    if settings.openai_compat_base_url:
        providers["openai_compat"] = OpenAICompatProvider(
            base_url=settings.openai_compat_base_url,
            api_key=settings.openai_compat_api_key or None,
        )
    if not providers:
        sys.exit(
            "ask.py needs a model provider: set ANTHROPIC_API_KEY (or "
            "OPENAI_COMPAT_BASE_URL) in .env — tests use MockProvider instead."
        )
    return providers


def _print_result(agent: str, result: RunResult) -> None:
    final = result.final
    print(
        f"\n─── {agent} · {result.status} · {result.iterations} iteration(s) · "
        f"${result.cost_usd} ───"
    )
    if final is None:
        print("(no final answer — see the trace)")
        return
    if final.abstained:
        print("[abstained]")
    print(final.answer)
    if final.citations:
        print("\ncitations: " + ", ".join(c.ref for c in final.citations))
    batch_id = getattr(final, "batch_id", None)
    if batch_id is not None:
        print(f"batch submitted: {batch_id}")


async def _amain(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ask", description="Ask the Backline agents.")
    parser.add_argument("question", help="the message to route or ask directly")
    parser.add_argument(
        "--agent",
        choices=["counsel", "analyst", "reconciler"],
        default=None,
        help="skip the router and ask one agent directly",
    )
    parser.add_argument("--model", default=None, help="override the planner model id")
    parser.add_argument("--router-model", default=None, help="override the router model id")
    args = parser.parse_args(argv)

    settings = get_settings()
    providers = _providers()
    registry = ModelRegistry.load()
    pool = await asyncpg.create_pool(settings.database_url, min_size=1, max_size=4)
    postgres_sink = PostgresSink(settings.database_url)
    sinks: list[TraceSink] = [
        JsonlSink(Path(settings.data_dir) / "traces"),
        postgres_sink,
    ]
    tracer = Tracer(sinks)
    ctx = ToolContext.create(pool)

    try:
        if args.agent is not None:
            agent = build_agent(args.agent, ctx, model=args.model)
            runtime = AgentRuntime(providers=providers, registry=registry, tracer=tracer)
            result = await runtime.run(agent, args.question)
            _print_result(args.agent, result)
            print(f"trace: run {result.run_id}")
            return 0 if result.status == "completed" else 1

        outcome = await route_and_run(
            args.question,
            ctx=ctx,
            providers=providers,
            registry=registry,
            tracer=tracer,
            agent_model=args.model,
            router_model=args.router_model,
        )
        decision: Any = outcome.decision
        print(
            f"routed to {decision.target} · confidence {decision.confidence:.2f}"
            + (f" · {decision.reason}" if decision.reason else "")
        )
        if outcome.decision.target == "clarify":
            print(f"\n{outcome.clarification}")
            return 0
        if outcome.recalled_notes:
            print("(entity notes were auto-recalled into context)")
        assert outcome.result is not None and outcome.agent is not None
        _print_result(outcome.agent, outcome.result)
        print(f"trace: run {outcome.result.run_id}")
        return 0 if outcome.result.status == "completed" else 1
    finally:
        await pool.close()
        await postgres_sink.aclose()


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(_amain(argv))


if __name__ == "__main__":
    sys.exit(main())
