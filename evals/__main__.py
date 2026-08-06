"""``python -m evals`` — the eval harness CLI (BUILD_PLAN §5.4).

    python -m evals generate [--check] [--load-db]     # build/verify the suite
    python -m evals run --suite core --model claude-sonnet-5 --budget 5.00 \
        [--track platform|b0|b1] [--gate-subset] [--judge MODEL] [--resume RUN_ID]
    python -m evals smoke [--write-baseline]           # keyless mock plumbing test
    python -m evals gate --summary PATH [--write-baseline]
    python -m evals report --summary PATH [PATH ...]

``run`` needs a seeded database and a real provider key (ANTHROPIC_API_KEY or an
OpenAI-compat endpoint); everything else is keyless. Every run traces to Postgres +
JSONL like production, so eval runs are inspectable in the Phase 6 Trace Inspector.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from decimal import Decimal
from pathlib import Path

from evals.types import Suite, load_suite


def _cmd_generate(argv: list[str]) -> int:
    from evals.generate_suite import main as generate_main

    return generate_main(argv)


def _cmd_smoke(argv: list[str]) -> int:
    from evals.smoke import main as smoke_main

    return smoke_main(argv)


async def _run_async(args: argparse.Namespace, suite: Suite) -> int:
    import asyncpg

    from backline.config import get_settings
    from backline.core.trace import JsonlSink, PostgresSink, TraceSink
    from backline.providers.anthropic import AnthropicProvider
    from backline.providers.base import Provider
    from backline.providers.openai_compat import OpenAICompatProvider
    from backline.providers.registry import ModelRegistry
    from evals.gate import evaluate_gate, load_baseline
    from evals.report import render_markdown
    from evals.runner import BudgetRefused, EvalRunner, RunnerConfig

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
        print(
            "evals run needs a provider: set ANTHROPIC_API_KEY or "
            "OPENAI_COMPAT_BASE_URL (use `evals smoke` for the keyless path).",
            file=sys.stderr,
        )
        return 2

    pool = await asyncpg.create_pool(settings.database_url, min_size=2, max_size=8)
    postgres_sink = PostgresSink(settings.database_url)
    # data_path/anchoring: artifact + trace paths resolve against the repo root,
    # never the CWD the CLI happened to be launched from (D-022).
    sinks: list[TraceSink] = [JsonlSink(settings.data_path / "traces"), postgres_sink]
    try:
        runner = EvalRunner(
            pool=pool,
            providers=providers,
            registry=ModelRegistry.load(),
            settings=settings,
            extra_sinks=sinks,
        )
        config = RunnerConfig(
            suite=suite,
            model=args.model,
            track=args.track,
            subset="gate" if args.gate_subset else None,
            categories=(
                tuple(part.strip() for part in args.categories.split(",") if part.strip())
                if args.categories
                else None
            ),
            budget_usd=Decimal(args.budget),
            assume_yes=args.yes,
            judge_model=(
                None
                if args.no_judge or args.track != "platform"
                else (args.judge or settings.judge_model)
            ),
            concurrency=args.concurrency,
            out_dir=Path(args.out),
            data_dir=settings.data_path,
            resume_run_id=uuid.UUID(args.resume) if args.resume else None,
            pack_tokens=args.pack_tokens,
        )
        try:
            summary = await runner.run(config)
        except BudgetRefused as refused:
            print(f"refused: {refused}", file=sys.stderr)
            return 2
        print()
        print(render_markdown(summary.as_dict()))
        print(f"artifacts → {summary.out_dir}")
        if args.gate:
            result = evaluate_gate(summary.as_dict(), load_baseline())
            print()
            print(result.render())
            return 0 if result.passed else 1
        return 0
    finally:
        await pool.close()
        await postgres_sink.aclose()


def _cmd_run(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="evals run", description="Run the eval suite.")
    parser.add_argument("--suite", default="core")
    parser.add_argument("--model", required=True)
    parser.add_argument("--track", choices=["platform", "b0", "b1"], default="platform")
    parser.add_argument(
        "--gate-subset", action="store_true", help="run only the budget-capped CI regression subset"
    )
    parser.add_argument(
        "--categories",
        default=None,
        help="comma-separated category filter for targeted re-runs "
        "(e.g. abstention,multi_step); a filtered summary is not gate-comparable",
    )
    parser.add_argument("--budget", default=None, help="USD hard cap (default: EVAL_BUDGET_USD)")
    parser.add_argument(
        "--yes", action="store_true", help="proceed even when the projection exceeds the budget"
    )
    parser.add_argument("--judge", default=None, help="T3 judge model (default: JUDGE_MODEL)")
    parser.add_argument("--no-judge", action="store_true", help="skip T3 entirely")
    parser.add_argument("--resume", default=None, help="eval run id to resume")
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--out", default="data/evals")
    parser.add_argument(
        "--pack-tokens", type=int, default=24_000, help="B0 context-packer token budget"
    )
    parser.add_argument(
        "--gate", action="store_true", help="also evaluate the regression gate; exit 1 on failure"
    )
    args = parser.parse_args(argv)
    if args.budget is None:
        from backline.config import get_settings

        args.budget = str(get_settings().eval_budget_usd)
    suite = load_suite(args.suite)
    return asyncio.run(_run_async(args, suite))


def _cmd_gate(argv: list[str]) -> int:
    from evals.gate import BASELINE_PATH, evaluate_gate, load_baseline, write_baseline
    from evals.report import load_summary

    parser = argparse.ArgumentParser(prog="evals gate", description="Regression gate (§5.4).")
    parser.add_argument("--summary", type=Path, required=True, help="summary.json from an eval run")
    parser.add_argument("--baseline", type=Path, default=BASELINE_PATH)
    parser.add_argument(
        "--write-baseline",
        action="store_true",
        help="record this summary as the new baseline for its shape",
    )
    parser.add_argument("--note", default="", help="note stored with --write-baseline")
    args = parser.parse_args(argv)

    summary = load_summary(args.summary)
    if args.write_baseline:
        entry = write_baseline(summary, path=args.baseline, note=args.note)
        print(
            f"baseline updated for ({entry['model']}, {entry['track']}, {entry['subset']}) "
            f"→ {args.baseline}"
        )
        return 0
    result = evaluate_gate(summary, load_baseline(args.baseline))
    print(result.render())
    return 0 if result.passed else 1


def _cmd_report(argv: list[str]) -> int:
    from evals.report import load_summary, render_compare, render_markdown

    parser = argparse.ArgumentParser(prog="evals report", description="Render results tables.")
    parser.add_argument("--summary", type=Path, nargs="+", required=True)
    parser.add_argument("--out", type=Path, default=None, help="write markdown here too")
    args = parser.parse_args(argv)

    summaries = [load_summary(path) for path in args.summary]
    text = render_markdown(summaries[0]) if len(summaries) == 1 else render_compare(summaries)
    print(text)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
        print(f"written → {args.out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    commands = {
        "generate": _cmd_generate,
        "run": _cmd_run,
        "smoke": _cmd_smoke,
        "gate": _cmd_gate,
        "report": _cmd_report,
    }
    if not argv or argv[0] in {"-h", "--help"}:
        print(__doc__)
        return 0
    command = commands.get(argv[0])
    if command is None:
        print(f"unknown command {argv[0]!r} — one of: {', '.join(commands)}", file=sys.stderr)
        return 2
    return command(argv[1:])


if __name__ == "__main__":
    sys.exit(main())
