"""``python benchmarks/run_sweep.py`` — the Phase 7 model benchmark sweep (§7).

    python benchmarks/run_sweep.py                     # the committed API matrix, in order
    python benchmarks/run_sweep.py --model local-qwen --budget 0 --yes   # LOCAL.md row
    python benchmarks/run_sweep.py --model claude-opus-5 --resume <eval_run_id>
    python benchmarks/run_sweep.py --subset smoke --yes    # 10-question live dry pass

Unattended and resumable: rows run sequentially (honest latencies — no cross-model
contention); each row's eval run id is recorded in ``data/benchmarks/sweep_state.json``
before its first question, so an interrupted sweep re-invoked with the same command
picks up exactly where it stopped. Per-row budgets are hard caps (sweep.yaml; the
operator's Opus cap is $35): a row that hits its cap is written out as a partial
result and the sweep exits 1 with the resume command printed.

Needs a seeded world (``make seed && make embed``) and a provider key —
``ANTHROPIC_API_KEY`` for the API rows, ``OPENAI_COMPAT_BASE_URL`` for the local row
(plus the Anthropic key for the pinned judge; ``--no-judge`` drops that requirement
but the row's scores stop being judge-comparable). Dry passes (``--subset``) never
write to ``benchmarks/results/``.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path

if __package__ is None or __package__ == "":  # `python benchmarks/run_sweep.py`
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from benchmarks.sweep import (
    DEFAULT_MATRIX_PATH,
    DEFAULT_RESULTS_DIR,
    RowOutcome,
    SweepContext,
    SweepRow,
    completed_results,
    default_state_path,
    load_matrix,
    preflight_world,
    run_row,
    validate_matrix_models,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="benchmarks/run_sweep.py",
        description="Model benchmark sweep: models x full eval suite (BUILD_PLAN §7).",
    )
    parser.add_argument("--matrix", type=Path, default=DEFAULT_MATRIX_PATH)
    parser.add_argument(
        "--model",
        default=None,
        help="run a single row (matrix rows, followups, or any registry model with --budget)",
    )
    parser.add_argument(
        "--budget",
        default=None,
        help="USD hard cap override for --model (0 = uncapped, zero-priced rows only)",
    )
    parser.add_argument(
        "--yes", action="store_true", help="proceed when the projection exceeds the budget"
    )
    parser.add_argument(
        "--fresh",
        action="store_true",
        help="ignore sweep state and prior results; start the row(s) from scratch",
    )
    parser.add_argument("--resume", default=None, help="eval run id to resume (needs --model)")
    parser.add_argument("--judge", default=None, help="override the matrix judge model")
    parser.add_argument(
        "--no-judge",
        action="store_true",
        help="skip T3 — the row stops being comparable with judged rows",
    )
    parser.add_argument(
        "--subset",
        choices=["smoke", "gate"],
        default=None,
        help="dry pass on a suite subset; prints results, writes nothing to benchmarks/results",
    )
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--out", type=Path, default=DEFAULT_RESULTS_DIR)
    parser.add_argument(
        "--no-report", action="store_true", help="skip regenerating REPORT.md after the rows"
    )
    return parser


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.budget is not None and args.model is None:
        parser.error("--budget needs --model (matrix rows carry their own budgets)")
    if args.resume is not None and args.model is None:
        parser.error("--resume needs --model")
    if args.judge is not None and args.no_judge:
        parser.error("--judge and --no-judge are mutually exclusive")
    return args


RowFinder = Callable[[str], SweepRow | None]


def resolve_rows(
    args: argparse.Namespace, matrix_rows: list[SweepRow], find: RowFinder
) -> list[SweepRow]:
    """The rows this invocation runs: the API matrix by default, one row with --model."""
    if args.model is None:
        return list(matrix_rows)
    row = find(args.model)
    if row is None:
        if args.budget is None:
            raise SystemExit(
                f"model {args.model!r} is not in the sweep matrix — pass --budget to run "
                f"an off-matrix registry model"
            )
        row = SweepRow(model=args.model, budget_usd=Decimal(args.budget))
    elif args.budget is not None:
        row = SweepRow(model=row.model, budget_usd=Decimal(args.budget))
    return [row]


async def _run(args: argparse.Namespace) -> int:
    import asyncpg

    from backline.config import get_settings
    from backline.core.trace import JsonlSink, PostgresSink, TraceSink
    from backline.providers.anthropic import AnthropicProvider
    from backline.providers.base import Provider
    from backline.providers.openai_compat import OpenAICompatProvider
    from backline.providers.registry import ModelRegistry
    from benchmarks.report import write_report
    from evals.report import render_markdown
    from evals.runner import BudgetRefused, EvalRunner
    from evals.types import load_suite

    matrix = load_matrix(args.matrix)
    registry = ModelRegistry.load()
    validate_matrix_models(matrix, registry)
    rows = resolve_rows(args, matrix.rows, matrix.find)

    settings = get_settings()
    providers: dict[str, Provider] = {}
    if settings.anthropic_api_key:
        providers["anthropic"] = AnthropicProvider(api_key=settings.anthropic_api_key)
    if settings.openai_compat_base_url:
        providers["openai_compat"] = OpenAICompatProvider(
            base_url=settings.openai_compat_base_url,
            api_key=settings.openai_compat_api_key or None,
        )

    # Fail before any spend: every requested row (and the judge) needs its provider.
    missing: list[str] = []
    for row in rows:
        needed = registry.get(row.model).provider
        if needed not in providers:
            missing.append(f"{row.model} needs provider {needed!r}")
    if not args.no_judge:
        judge = args.judge or matrix.judge_model
        needed = registry.get(judge).provider
        if needed not in providers:
            missing.append(f"judge {judge} needs provider {needed!r} (or pass --no-judge)")
    if missing:
        print(
            "sweep refused — providers not configured:\n  " + "\n  ".join(missing) + "\n"
            "Set ANTHROPIC_API_KEY and/or OPENAI_COMPAT_BASE_URL in .env.",
            file=sys.stderr,
        )
        return 2

    suite = load_suite(matrix.suite)
    pool = await asyncpg.create_pool(settings.database_url, min_size=2, max_size=8)
    postgres_sink = PostgresSink(settings.database_url)
    sinks: list[TraceSink] = [JsonlSink(settings.data_path / "traces"), postgres_sink]
    try:
        problems = await preflight_world(pool)
        if problems:
            print("sweep refused — world not ready:\n  " + "\n  ".join(problems), file=sys.stderr)
            return 2

        runner = EvalRunner(
            pool=pool,
            providers=providers,
            registry=registry,
            settings=settings,
            extra_sinks=sinks,
        )
        ctx = SweepContext(
            pool=pool,
            registry=registry,
            settings=settings,
            suite=suite,
            matrix=matrix,
            runner=runner,
            results_dir=args.out,
            state_file=default_state_path(settings),
            concurrency=args.concurrency,
        )

        outcomes: list[RowOutcome] = []
        for index, row in enumerate(rows, start=1):
            cap = "uncapped" if row.uncapped else f"${row.budget_usd}"
            print(f"\n=== sweep row {index}/{len(rows)}: {row.model} · cap {cap} ===")
            if not args.fresh and args.subset is None:
                done = completed_results(args.out, row.model, suite.suite_hash)
                if done is not None:
                    print(
                        f"[{row.model}] complete results for suite {suite.suite_hash} "
                        f"already committed (run {str(done['eval_run_id'])[:8]}) — "
                        f"skipping; pass --fresh to re-measure"
                    )
                    continue
            try:
                outcome = await run_row(
                    ctx,
                    row,
                    resume_run_id=uuid.UUID(args.resume) if args.resume else None,
                    assume_yes=args.yes,
                    judge_model=args.judge,
                    no_judge=args.no_judge,
                    subset=args.subset,
                    fresh=args.fresh,
                )
            except BudgetRefused as refused:
                # A refusal means projections moved past the committed caps — stop
                # the sweep loudly instead of skipping ahead on stale expectations.
                print(f"refused: {refused}", file=sys.stderr)
                return 2
            outcomes.append(outcome)
            print()
            print(render_markdown(outcome.summary.as_dict()))
            if outcome.path is not None:
                print(f"results → {outcome.path}")

        if args.subset is None and not args.no_report:
            report_path = write_report(matrix, args.out)
            print(f"report → {report_path}")

        total = sum((o.summary.total_cost_usd for o in outcomes), Decimal("0"))
        partial = [o for o in outcomes if not o.complete]
        print(f"\nsweep spend this invocation: ${total}")
        if partial:
            for o in partial:
                print(
                    f"partial: {o.row.model} scored {o.summary.n_scored}/"
                    f"{o.summary.n_questions} — resume with\n"
                    f"  python benchmarks/run_sweep.py --model {o.row.model} "
                    f"--resume {o.summary.eval_run_id}"
                )
            return 1
        return 0
    finally:
        await pool.close()
        await postgres_sink.aclose()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        print(
            "\ninterrupted — sweep state is saved; re-run the same command to resume",
            file=sys.stderr,
        )
        return 130


if __name__ == "__main__":
    sys.exit(main())
