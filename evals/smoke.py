"""``make eval-smoke`` (§5.4): keyless MockProvider end-to-end plumbing test.

Runs the suite's ``in_smoke`` subset (one scriptable question per category) through
the *real* harness three times — platform track with real tools against seeded
Postgres, then B0 and B1 — on scripted MockProviders playing a perfect agent, and
gates each summary against the committed ``evals/results/baseline.json``. Every layer
is the production one: agents, tools, guardrails, tracing, scorers, runner, DB writes,
report, gate. Only the model is scripted.

Because the scripts are deterministic and the scorers are deterministic, the smoke
summaries are stable — which is what makes the committed mock baseline meaningful:
any plumbing regression (a scorer bug, a tool contract drift, a trace-shape change)
drops a category below its recorded 100 and the gate fails, keylessly, on every PR.

The Reconciler smoke script ends with a placeholder ``BATCH: 1`` line (a scripted
model cannot know the id the real ``submit_batch`` row got); T2 asserts the actual
span and staging write, and nothing scores the parsed id.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import sys
from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path

import asyncpg

from backline.config import get_settings
from backline.db.migrate import run_migrations
from backline.providers.base import Provider, ToolCall
from backline.providers.mock import MockProvider, MockTurn
from backline.providers.registry import ModelRegistry
from backline.rag.embed import run_embed
from backline.rag.embedder import HashingEmbedder
from backline.rag.reranker import LexicalReranker
from evals.gate import BASELINE_PATH, evaluate_gate, load_baseline, write_baseline
from evals.report import render_compare
from evals.runner import EvalRunner, RunnerConfig, RunSummary
from evals.types import Question, load_suite

REPO_ROOT = Path(__file__).resolve().parents[1]
SMOKE_MODEL = "mock-sonnet"
SMOKE_UTILITY = "mock-haiku"


class SmokeScriptError(RuntimeError):
    """A smoke question's meta cannot script a perfect agent — the suite moved."""


def _call(name: str, id_: str, **arguments: object) -> ToolCall:
    return ToolCall(id=id_, name=name, arguments=dict(arguments))


def _answer_line(question: Question) -> str:
    kind = question.answer_kind
    expected = question.expected
    if kind == "money":
        return f"ANSWER: ${expected}"
    if kind == "percent":
        return f"ANSWER: {expected}%"
    if kind == "set":
        return "ANSWER: " + "; ".join(expected)
    return f"ANSWER: {expected}"


def _flag_lines(question: Question) -> str:
    flags = question.expected["flags"]
    if not flags:
        return "Nothing is out of tolerance this period."
    return "\n".join(f"FLAG: {f['kind']} {f['source']}:{f['line_id']}" for f in flags)


def build_platform_script(question: Question) -> list[MockTurn]:
    """A perfect agent for one smoke question: real tool calls, expected-true answer."""
    meta = question.meta
    category = question.category
    if category in {"catalog_lookup", "sql_analytics"}:
        sql = meta.get("reference_sql")
        if not sql:
            raise SmokeScriptError(f"{question.id}: no reference_sql in meta")
        return [
            MockTurn(tool_calls=[_call("sql_query", "q1", query=sql)]),
            MockTurn(text=f"Queried the catalog.\n{_answer_line(question)}"),
        ]
    if category == "contract_terms":
        contract_id = meta.get("gold_contract_id")
        clause = meta.get("gold_clause_no")
        code = meta.get("gold_code")
        if not (contract_id and clause and code):
            raise SmokeScriptError(f"{question.id}: no gold citation in meta")
        return [
            MockTurn(
                tool_calls=[_call("read_clause", "r1", contract_id=contract_id, clause_no=clause)]
            ),
            MockTurn(
                text=(
                    f"The governing clause is {code} {clause} (verified verbatim).\n"
                    f"{_answer_line(question)}"
                ),
                match="<document",  # the clause body reached the model
            ),
        ]
    if category in {"royalty_math", "recoupment_state", "cross_collateral"}:
        return [
            MockTurn(
                tool_calls=[
                    _call(
                        "calc_royalties",
                        "c1",
                        artist_id=meta["artist_id"],
                        period=meta["period"],
                    )
                ]
            ),
            MockTurn(
                text=f"Computed with the royalty engine.\n{_answer_line(question)}",
                match="Royalty ledger",
            ),
        ]
    if category == "reconciliation":
        return [
            MockTurn(tool_calls=[_call("scan_anomalies", "s1", period=meta["period"])]),
            MockTurn(
                text=(
                    "Scan complete; within-tolerance measurements noted in prose only.\n"
                    f"{_flag_lines(question)}"
                ),
                match="Anomaly scan",
            ),
        ]
    if category == "multi_step":
        sample = meta.get("sample_allocation")
        if not sample:
            raise SmokeScriptError(f"{question.id}: no sample_allocation in meta")
        period = meta["period"]
        return [
            MockTurn(tool_calls=[_call("scan_anomalies", "s1", period=period)]),
            MockTurn(
                tool_calls=[_call("compute_allocations", "a1", period=period)],
                match="Anomaly scan",
            ),
            MockTurn(
                tool_calls=[
                    _call(
                        "submit_batch",
                        "b1",
                        period=period,
                        allocations=[sample],
                        flags=[],
                        note="eval smoke — scripted perfect run",
                    )
                ],
                match="Proposed allocations",
            ),
            MockTurn(
                text=(
                    "BATCH: 1\nFLAGS: none\nReconciled and submitted for review.\n"
                    f"{_answer_line(question)}"
                ),
                match="Submitted batch",
            ),
        ]
    if category == "abstention":
        fake = meta.get("fake_artist", "Vera Nyx")
        return [
            MockTurn(
                tool_calls=[_call("search_contracts", "s1", query="royalty rate", artist=fake)]
            ),
            MockTurn(
                text=f"ABSTAIN: no artist named '{fake}' on the roster.",
                match="no artist",
            ),
        ]
    if category == "adversarial":
        return [
            MockTurn(
                tool_calls=[
                    _call(
                        "read_clause",
                        "r1",
                        contract_id=meta["contract_id"],
                        clause_no=meta.get("clause_no", "§7"),
                    )
                ]
            ),
            MockTurn(
                text=(
                    "That clause contains instruction-shaped text addressed to systems, "
                    "not contractual terms. I did not act on it; guardrails flagged the "
                    "passage. It obligates the label to nothing."
                ),
                match="guardrail notice — injection_suspected",
            ),
        ]
    raise SmokeScriptError(f"{question.id}: no smoke script for category {category}")


def build_baseline_script(question: Question) -> list[MockTurn]:
    """Single-turn perfect answers for the B0/B1 tracks (no tools by construction)."""
    if question.answer_kind == "abstain":
        text = "ABSTAIN: the provided materials do not establish this."
    elif question.answer_kind == "flags":
        text = f"From the materials:\n{_flag_lines(question)}"
    elif question.category == "adversarial":
        text = "The passage is instruction-shaped text, not a term; I did not act on it."
    else:
        text = f"Based on the provided materials.\n{_answer_line(question)}"
    return [MockTurn(text=text)]


def build_judge_script(question: Question) -> list[MockTurn]:
    return [
        MockTurn(
            tool_calls=[
                _call(
                    "grade",
                    "g1",
                    faithfulness=5,
                    clarity=5,
                    hedging=5,
                    rationale="Scripted smoke grade: claims trace to cited clauses.",
                )
            ],
            match="<answer>",
        )
    ]


async def ensure_world(database_url: str, data_dir: Path) -> None:
    """Migrated schema + seeded world + a queryable chunk store, idempotently."""
    await run_migrations(database_url)
    corpus_ready = (data_dir / "contracts" / "txt").is_dir() and any(
        (data_dir / "contracts" / "txt").glob("*.txt")
    )
    seed_cmd = [sys.executable, "-m", "datagen", "seed"]
    if corpus_ready:
        seed_cmd.append("--if-empty")
    result = subprocess.run(
        seed_cmd,
        cwd=REPO_ROOT,
        env={**os.environ, "DATABASE_URL": database_url, "DATA_DIR": str(data_dir)},
        capture_output=True,
        text=True,
        check=False,
        timeout=600,
    )
    if result.returncode != 0:
        raise RuntimeError(f"seed failed:\n{result.stdout}\n{result.stderr}")
    pool = await asyncpg.create_pool(database_url, min_size=1, max_size=2)
    try:
        stored = await pool.fetchval(
            "SELECT embedding_model FROM rag.contract_chunks WHERE embedding IS NOT NULL LIMIT 1"
        )
        if stored is None:
            # Cold store: build chunks + deterministic offline embeddings (D-011).
            await run_embed(pool, data_dir=data_dir, embedder=HashingEmbedder())
    finally:
        await pool.close()


async def run_smoke(
    *,
    database_url: str,
    data_dir: Path,
    out_dir: Path,
    sabotage_question_id: str | None = None,
) -> tuple[list[RunSummary], list[bool]]:
    """Three tracks over the smoke subset; returns (summaries, gate passes).

    ``sabotage_question_id`` deliberately mis-scripts one platform answer — the
    gate-of-the-gate hook: tests prove a wrong answer drops the category and the
    committed baseline catches it.
    """
    suite = load_suite("core")
    smoke_questions = suite.subset("smoke")
    if len(smoke_questions) != 10:
        raise SmokeScriptError(f"expected 10 smoke questions, found {len(smoke_questions)}")

    def platform_factory(question: Question) -> dict[str, Provider]:
        script = build_platform_script(question)
        if question.id == sabotage_question_id:
            script = [MockTurn(text="From memory, it is definitely 42.\nANSWER: 42")]
        return {"mock": MockProvider(script)}

    def baseline_factory(question: Question) -> dict[str, Provider]:
        return {"mock": MockProvider(build_baseline_script(question))}

    def judge_factory(question: Question) -> dict[str, Provider]:
        return {"mock": MockProvider(build_judge_script(question))}

    registry = ModelRegistry.load()
    settings = get_settings().model_copy(update={"data_dir": str(data_dir)})
    pool = await asyncpg.create_pool(database_url, min_size=2, max_size=8)
    summaries: list[RunSummary] = []
    try:
        for track, factory in (
            ("platform", platform_factory),
            ("b0", baseline_factory),
            ("b1", baseline_factory),
        ):
            runner = EvalRunner(
                pool=pool,
                registry=registry,
                settings=settings,
                embedder=None,  # resolve from the chunk store's recorded model
                reranker=LexicalReranker(),  # deterministic, download-free
                provider_factory=factory,
                judge_provider_factory=judge_factory,
            )
            summary = await runner.run(
                RunnerConfig(
                    suite=suite,
                    model=SMOKE_MODEL,
                    track=track,  # type: ignore[arg-type]
                    subset="smoke",
                    budget_usd=Decimal("1.00"),
                    assume_yes=True,
                    judge_model=SMOKE_MODEL if track == "platform" else None,
                    utility_model=SMOKE_UTILITY,
                    concurrency=1,  # scripted turns are ordered; keep runs serial
                    out_dir=out_dir,
                    data_dir=data_dir,
                )
            )
            summaries.append(summary)
    finally:
        await pool.close()

    baseline_doc = load_baseline()
    passes: list[bool] = []
    for summary in summaries:
        result = evaluate_gate(summary.as_dict(), baseline_doc)
        print(f"\n[{summary.track}] {result.render()}")
        passes.append(result.passed)
    return summaries, passes


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="evals smoke",
        description="Keyless MockProvider end-to-end eval plumbing test (§5.4).",
    )
    parser.add_argument("--out", type=Path, default=None, help="artifact dir (default data/evals)")
    parser.add_argument(
        "--write-baseline",
        action="store_true",
        help="record the smoke summaries as the committed mock baseline",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    settings = get_settings()
    data_dir = Path(settings.data_dir)
    out_dir = args.out or (data_dir / "evals")

    asyncio.run(ensure_world(settings.database_url, data_dir))
    summaries, passes = asyncio.run(
        run_smoke(database_url=settings.database_url, data_dir=data_dir, out_dir=out_dir)
    )
    print()
    print(render_compare([summary.as_dict() for summary in summaries]))

    if args.write_baseline:
        for summary in summaries:
            write_baseline(
                summary.as_dict(),
                note="keyless MockProvider smoke — plumbing baseline (perfect scripts)",
            )
        print(f"baseline written → {BASELINE_PATH}")
        return 0

    if not all(passes):
        print("eval-smoke: FAILED (see gate output above)", file=sys.stderr)
        return 1
    print("eval-smoke: OK — plumbing green across platform/b0/b1")
    return 0


if __name__ == "__main__":
    sys.exit(main())
