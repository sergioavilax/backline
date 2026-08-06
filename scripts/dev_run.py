"""Demo: a scripted mock agent end-to-end through the Phase 2 platform core.

Runs the full loop — MockProvider script, tool execution with guardrails, dedup,
tracing to JSONL — and prints the span tree the run produced. No API key, no network.

    uv run python scripts/dev_run.py             # JSONL trace only
    uv run python scripts/dev_run.py --postgres  # also persist to app.runs/app.spans
                                                 # (needs DATABASE_URL + migrations)
"""

import argparse
import asyncio
import sys
from decimal import Decimal
from pathlib import Path

from pydantic import BaseModel

from backline.config import get_settings
from backline.core.guardrails import RunLimits
from backline.core.runtime import AgentRuntime, AgentSpec, RunResult, Tool
from backline.core.trace import (
    InMemorySink,
    JsonlSink,
    PostgresSink,
    SpanRecord,
    Tracer,
    TraceSink,
)
from backline.providers.base import ToolCall
from backline.providers.mock import MockProvider, MockTurn
from backline.providers.registry import ModelRegistry


class LookupArtistParams(BaseModel):
    stage_name: str


class RoyaltyRateParams(BaseModel):
    artist_id: int
    revenue_type: str


async def lookup_artist(params: LookupArtistParams) -> str:
    return f"artist {params.stage_name!r} found: id=42, imprint=Foldback Records"


async def royalty_rate(params: RoyaltyRateParams) -> str:
    return (
        f"artist {params.artist_id} rate for {params.revenue_type}: 30% worldwide "
        f"(base contract FBR-C-00042 §3)"
    )


def demo_agent() -> AgentSpec:
    return AgentSpec(
        name="demo-agent",
        system_prompt=(
            "You answer questions about Foldback Records artists using the tools. "
            "Never do royalty math in your head."
        ),
        model="mock-sonnet",
        tools=[
            Tool(
                name="lookup_artist",
                description="Find an artist by stage name.",
                params=LookupArtistParams,
                handler=lookup_artist,
            ),
            Tool(
                name="royalty_rate",
                description="Look up an artist's royalty rate for a revenue type.",
                params=RoyaltyRateParams,
                handler=royalty_rate,
            ),
        ],
        limits=RunLimits.from_settings(),
    )


def scripted_provider() -> MockProvider:
    """The canned three-turn conversation the demo agent plays out."""
    return MockProvider(
        [
            MockTurn(
                text="I'll find the artist first.",
                tool_calls=[
                    ToolCall(id="c1", name="lookup_artist", arguments={"stage_name": "Nova Reyes"})
                ],
                match="Nova Reyes",
            ),
            MockTurn(
                text="Now the streaming rate.",
                tool_calls=[
                    ToolCall(
                        id="c2",
                        name="royalty_rate",
                        arguments={"artist_id": 42, "revenue_type": "streaming"},
                    )
                ],
                match="id=42",
            ),
            MockTurn(
                text=(
                    "Nova Reyes (artist 42, Foldback Records) earns 30% on streaming "
                    "worldwide, per base contract FBR-C-00042 §3."
                ),
                match="30% worldwide",
            ),
        ]
    )


def print_span_tree(sink: InMemorySink, result: RunResult) -> None:
    spans = sink.spans
    top = [s for s in spans if s.parent_id is None]
    top.sort(key=lambda s: s.started_at)
    children: dict[object, list[SpanRecord]] = {}
    for span in spans:
        if span.parent_id is not None:
            children.setdefault(span.parent_id, []).append(span)
    for kids in children.values():
        kids.sort(key=lambda s: s.started_at)

    print(
        f"run {result.run_id} agent=demo-agent status={result.status} "
        f"iterations={result.iterations} cost=${result.cost_usd}"
    )
    for i, span in enumerate(top):
        last_top = i == len(top) - 1
        print(f"{'└─' if last_top else '├─'} {span.kind:<11} {span.name}")
        kids = children.get(span.id, [])
        for j, kid in enumerate(kids):
            stem = "   " if last_top else "│  "
            branch = "└─" if j == len(kids) - 1 else "├─"
            attrs = kid.attrs
            detail = ""
            if kid.kind == "llm_call":
                detail = (
                    f" in={attrs.get('gen_ai.usage.input_tokens')}"
                    f" out={attrs.get('gen_ai.usage.output_tokens')}"
                    f" ${attrs.get('cost_usd')}"
                )
            elif kid.kind == "tool_call":
                detail = f" status={attrs.get('status')}"
            print(f"{stem}{branch} {kid.kind:<11} {kid.name}{detail}")


async def main(use_postgres: bool) -> int:
    settings = get_settings()
    traces_dir = Path(settings.data_dir) / "traces"
    memory = InMemorySink()
    sinks: list[TraceSink] = [memory, JsonlSink(traces_dir)]
    postgres: PostgresSink | None = None
    if use_postgres:
        postgres = PostgresSink()
        sinks.append(postgres)

    runtime = AgentRuntime(
        providers={"mock": scripted_provider()},
        registry=ModelRegistry.load(),
        tracer=Tracer(sinks),
    )
    result = await runtime.run(demo_agent(), "What is Nova Reyes' streaming royalty rate?")
    if postgres is not None:
        await postgres.aclose()

    print_span_tree(memory, result)
    print()
    if result.final is not None:
        print(f"final answer: {result.final.answer}")
    print(f"trace file:   {traces_dir / f'{result.run_id}.jsonl'}")
    if use_postgres:
        print("persisted:    app.runs + app.spans")

    if result.status != "completed" or result.cost_usd <= Decimal("0"):
        print(f"UNEXPECTED: status={result.status} cost={result.cost_usd}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--postgres",
        action="store_true",
        help="also persist the trace to app.runs/app.spans (needs DATABASE_URL)",
    )
    args = parser.parse_args()
    sys.exit(asyncio.run(main(use_postgres=args.postgres)))
