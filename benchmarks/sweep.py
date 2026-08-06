"""Phase 7 model benchmark sweep — core library (BUILD_PLAN §7).

``benchmarks/run_sweep.py`` drives this module: iterate the committed sweep matrix
(``benchmarks/sweep.yaml``) over the full eval suite — one ``EvalRunner`` run per
model row — and distill each run into ``benchmarks/results/{model}.json``:
accuracy by category, $/query from the CostMeter, p50/p95 latency, mean
iterations, tool-error rate. Unattended runs are resumable at both levels: the
sweep pre-mints each row's eval run id and records it in a state file under
``data/benchmarks/`` before any question runs, and the runner itself skips
already-scored questions when re-entered with that id.

Sweep policy (operator, 2026-08-06): the API rows run first — claude-opus-5 under
a hard $35 budget, then claude-sonnet-5 and claude-haiku-4-5; the local
OpenAI-compat row is a follow-up the operator executes per ``benchmarks/LOCAL.md``
(``--budget 0`` = uncapped: the row is zero-priced, so a dollar gate is
meaningless). The report degrades gracefully to API-only (§7).

Methodology (D-031): every row measures the *shipped platform* with only the
planner model swapped — same prompts, tools, per-run caps (D-020), utility model,
and one judge pinned across rows. ``usd_per_query`` is the agent loop's metered
cost alone; judge spend is harness overhead, carried separately.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_EVEN, Decimal
from pathlib import Path
from typing import Any, Literal

import asyncpg
import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from backline.config import Settings
from backline.jsonutil import canonical_dumps
from backline.providers.registry import ModelRegistry
from evals.runner import EvalRunner, RunnerConfig, RunSummary, git_sha
from evals.types import Suite

BENCHMARKS_DIR = Path(__file__).resolve().parent
DEFAULT_MATRIX_PATH = BENCHMARKS_DIR / "sweep.yaml"
DEFAULT_RESULTS_DIR = BENCHMARKS_DIR / "results"

UNCAPPED = Decimal("Infinity")

_QUERY_QUANTUM = Decimal("0.0001")  # $/query display quantum (sub-cent models exist)


class SweepRow(BaseModel):
    """One model row of the matrix. ``budget_usd`` is that row's whole-run hard cap."""

    model_config = ConfigDict(frozen=True)

    model: str
    budget_usd: Decimal

    @field_validator("budget_usd", mode="before")
    @classmethod
    def _no_float_budgets(cls, value: object) -> object:
        if isinstance(value, float):
            raise ValueError(
                "budget parsed as float — quote it in sweep.yaml (money is never float)"
            )
        return value

    @property
    def uncapped(self) -> bool:
        return self.budget_usd == 0

    @property
    def runner_budget(self) -> Decimal:
        """The cap handed to the eval runner. A zero budget marks a zero-priced row
        (the local endpoint): the runner's gate reads ``spent + reserved >= budget``,
        so passing 0 through would skip every question — uncapped is the meaning."""
        return UNCAPPED if self.uncapped else self.budget_usd


class SweepMatrix(BaseModel):
    model_config = ConfigDict(frozen=True)

    suite: str
    track: Literal["platform"]  # the sweep measures the platform; B0/B1 were Phase 5
    judge_model: str
    rows: list[SweepRow]
    followups: list[SweepRow] = Field(default_factory=list)

    def find(self, model: str) -> SweepRow | None:
        for row in [*self.rows, *self.followups]:
            if row.model == model:
                return row
        return None

    @property
    def model_ids(self) -> list[str]:
        return [row.model for row in [*self.rows, *self.followups]]


def load_matrix(path: Path = DEFAULT_MATRIX_PATH) -> SweepMatrix:
    raw: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    return SweepMatrix.model_validate(raw)


def validate_matrix_models(matrix: SweepMatrix, registry: ModelRegistry) -> None:
    unknown = [model for model in matrix.model_ids if model not in registry]
    if unknown:
        raise ValueError(f"sweep matrix names models missing from config/models.yaml: {unknown}")
    if matrix.judge_model not in registry:
        raise ValueError(f"judge model {matrix.judge_model!r} missing from config/models.yaml")


# ── sweep state (crash-safe resume across invocations) ───────────────────────────


def default_state_path(settings: Settings) -> Path:
    return settings.data_path / "benchmarks" / "sweep_state.json"


def load_state(path: Path) -> dict[str, dict[str, str]]:
    if not path.exists():
        return {}
    import json

    doc: dict[str, dict[str, str]] = json.loads(path.read_text(encoding="utf-8"))
    return doc


def save_state(path: Path, state: dict[str, dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(canonical_dumps(state) + "\n", encoding="utf-8")


# ── span-derived metrics (mean iterations, tool-error rate, agent-only cost) ─────


class RunAggregates(BaseModel):
    """Per-eval-run aggregates over the agent runs it spawned (``app.runs`` +
    ``app.spans``). Judge runs are separate rows (``agent='judge'``) and are
    deliberately outside these sums — that is what makes the agent/judge split."""

    model_config = ConfigDict(frozen=True)

    runs: int = 0
    runs_completed: int = 0
    runs_exhausted: int = 0
    runs_error: int = 0
    agent_cost_usd: Decimal = Decimal("0")
    iterations: int = 0
    tool_calls: int = 0
    tool_calls_by_status: dict[str, int] = Field(default_factory=dict)
    tokens_in: int = 0
    tokens_out: int = 0

    @property
    def tool_errors(self) -> int:
        return self.tool_calls - self.tool_calls_by_status.get("ok", 0)

    @property
    def tool_error_rate(self) -> float:
        return round(self.tool_errors / self.tool_calls, 4) if self.tool_calls else 0.0

    @property
    def iterations_mean(self) -> float:
        return round(self.iterations / self.runs, 2) if self.runs else 0.0


async def collect_run_metrics(pool: asyncpg.Pool, eval_run_id: uuid.UUID) -> RunAggregates:
    """Aggregate the eval run's agent runs from the trace store.

    The per-question ``run_id`` rides in every ``app.eval_results`` detail row, so
    resumed runs aggregate across all their sessions. Questions that died before a
    run existed (``harness_error``) carry no run id and are excluded — consistent
    with the summary, which records them at zero cost.
    """
    run_ids: list[uuid.UUID] = [
        row["run_id"]
        for row in await pool.fetch(
            "SELECT DISTINCT (detail->>'run_id')::uuid AS run_id "
            "FROM app.eval_results "
            "WHERE eval_run_id = $1 AND detail->>'run_id' IS NOT NULL",
            eval_run_id,
        )
    ]
    if not run_ids:
        return RunAggregates()

    by_status = {
        row["status"]: (int(row["n"]), Decimal(row["cost"]))
        for row in await pool.fetch(
            "SELECT status, count(*) AS n, coalesce(sum(cost_usd), 0) AS cost "
            "FROM app.runs WHERE id = ANY($1::uuid[]) GROUP BY status",
            run_ids,
        )
    }
    span_rows = await pool.fetch(
        "SELECT kind, coalesce(attrs->>'status', 'ok') AS status, count(*) AS n, "
        "  coalesce(sum((attrs->>'gen_ai.usage.input_tokens')::bigint), 0) AS tokens_in, "
        "  coalesce(sum((attrs->>'gen_ai.usage.output_tokens')::bigint), 0) AS tokens_out "
        "FROM app.spans WHERE run_id = ANY($1::uuid[]) AND ended_at IS NOT NULL "
        "GROUP BY 1, 2",
        run_ids,
    )
    iterations = 0
    tokens_in = 0
    tokens_out = 0
    tool_calls_by_status: dict[str, int] = {}
    for row in span_rows:
        if row["kind"] == "iteration":
            iterations += int(row["n"])
        elif row["kind"] == "llm_call":
            tokens_in += int(row["tokens_in"])
            tokens_out += int(row["tokens_out"])
        elif row["kind"] == "tool_call":
            status = str(row["status"])
            tool_calls_by_status[status] = tool_calls_by_status.get(status, 0) + int(row["n"])

    return RunAggregates(
        runs=sum(n for n, _ in by_status.values()),
        runs_completed=by_status.get("completed", (0, Decimal("0")))[0],
        runs_exhausted=by_status.get("exhausted", (0, Decimal("0")))[0],
        runs_error=by_status.get("error", (0, Decimal("0")))[0],
        agent_cost_usd=sum((cost for _, cost in by_status.values()), Decimal("0")),
        iterations=iterations,
        tool_calls=sum(tool_calls_by_status.values()),
        tool_calls_by_status=dict(sorted(tool_calls_by_status.items())),
        tokens_in=tokens_in,
        tokens_out=tokens_out,
    )


# ── the per-model results document (benchmarks/results/{model}.json) ─────────────


def results_file(results_dir: Path, model: str) -> Path:
    return results_dir / f"{model}.json"


def overall_score(categories: dict[str, Any]) -> float:
    total = sum(int(bucket["n"]) for bucket in categories.values())
    if not total:
        return 0.0
    weighted = sum(float(bucket["score"]) * int(bucket["n"]) for bucket in categories.values())
    return round(weighted / total, 2)


def build_results_doc(
    summary: dict[str, Any],
    metrics: RunAggregates,
    *,
    registry: ModelRegistry,
    row: SweepRow,
    suite_name: str,
    settings: Settings,
    concurrency: int,
    recorded_at: str | None = None,
) -> dict[str, Any]:
    """Distill one eval run summary + trace aggregates into the committed shape."""
    info = registry.get(summary["model"])
    n_scored = int(summary["n_scored"])
    total_cost = Decimal(str(summary["total_cost_usd"]))
    agent_cost = metrics.agent_cost_usd
    judge_cost = total_cost - agent_cost
    complete = n_scored == int(summary["n_questions"]) and not summary.get("budget_exhausted")

    def per_query(amount: Decimal) -> str | None:
        if not n_scored:
            return None
        return str((amount / n_scored).quantize(_QUERY_QUANTUM, rounding=ROUND_HALF_EVEN))

    return {
        "model": summary["model"],
        "provider": info.provider,
        "price": {
            "usd_per_mtok_in": str(info.usd_per_mtok_in),
            "usd_per_mtok_out": str(info.usd_per_mtok_out),
            "note": info.price_note,
        },
        "suite": suite_name,
        "suite_hash": summary["suite_hash"],
        "track": summary["track"],
        "subset": summary.get("subset"),
        "eval_run_id": summary["eval_run_id"],
        "git_sha": summary.get("git_sha"),
        "recorded_at": recorded_at or datetime.now(UTC).date().isoformat(),
        "judge": summary.get("judge"),
        "n_questions": int(summary["n_questions"]),
        "n_scored": n_scored,
        "n_skipped_budget": int(summary.get("n_skipped_budget", 0)),
        "budget_usd": str(row.budget_usd) if not row.uncapped else "0 (uncapped)",
        "budget_exhausted": bool(summary.get("budget_exhausted")),
        "complete": complete,
        "categories": summary["categories"],
        "overall_score": overall_score(summary["categories"]),
        "total_cost_usd": str(total_cost),
        "agent_cost_usd": str(agent_cost),
        "judge_cost_usd": str(judge_cost),
        "usd_per_query": per_query(agent_cost),
        "usd_per_query_with_judge": per_query(total_cost),
        "latency_ms_p50": int(summary["latency_ms_p50"]),
        "latency_ms_p95": int(summary["latency_ms_p95"]),
        "iterations_mean": metrics.iterations_mean,
        "runs": {
            "n": metrics.runs,
            "completed": metrics.runs_completed,
            "exhausted": metrics.runs_exhausted,
            "error": metrics.runs_error,
        },
        "tool_calls": {
            "n": metrics.tool_calls,
            "errors": metrics.tool_errors,
            "error_rate": metrics.tool_error_rate,
            "by_status": metrics.tool_calls_by_status,
        },
        "tokens": {"input": metrics.tokens_in, "output": metrics.tokens_out},
        "t2_violations": int(summary.get("t2_violations", 0)),
        "runtime_config": {
            "utility_model": settings.utility_model,
            "run_budget_usd": str(settings.run_budget_usd),
            "max_iterations": settings.max_iterations,
            "concurrency": concurrency,
        },
    }


def write_results_doc(results_dir: Path, doc: dict[str, Any]) -> Path:
    path = results_file(results_dir, str(doc["model"]))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(canonical_dumps(doc) + "\n", encoding="utf-8")
    return path


def load_results_doc(path: Path) -> dict[str, Any]:
    import json

    doc: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    if "model" not in doc or "categories" not in doc:
        raise ValueError(f"{path} is not a sweep results document")
    return doc


def completed_results(results_dir: Path, model: str, suite_hash: str) -> dict[str, Any] | None:
    """The committed results doc for this model iff it is complete *and* answers the
    current suite — the re-invoked sweep's skip-done check (``--fresh`` overrides)."""
    path = results_file(results_dir, model)
    if not path.exists():
        return None
    try:
        doc = load_results_doc(path)
    except ValueError:
        return None
    if doc.get("complete") and doc.get("suite_hash") == suite_hash:
        return doc
    return None


# ── world pre-flight (an unattended run must not burn budget on an empty world) ──


async def preflight_world(pool: asyncpg.Pool) -> list[str]:
    problems: list[str] = []
    artists = await pool.fetchval("SELECT count(*) FROM label.artists")
    if not artists:
        problems.append("world is not seeded (label.artists is empty) — run `make seed`")
    chunks = await pool.fetchval(
        "SELECT count(*) FROM rag.contract_chunks WHERE embedding IS NOT NULL"
    )
    if not chunks:
        problems.append("chunk store has no embeddings (rag.contract_chunks) — run `make embed`")
    return problems


# ── row execution ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SweepContext:
    """Everything shared across rows in one sweep invocation."""

    pool: asyncpg.Pool
    registry: ModelRegistry
    settings: Settings
    suite: Suite
    matrix: SweepMatrix
    runner: EvalRunner
    results_dir: Path = DEFAULT_RESULTS_DIR
    state_file: Path | None = None
    out_dir: Path = Path("data/evals")
    concurrency: int = 4


@dataclass(frozen=True)
class RowOutcome:
    row: SweepRow
    summary: RunSummary
    doc: dict[str, Any] | None  # None for --subset dry passes (never written)
    path: Path | None

    @property
    def complete(self) -> bool:
        return (
            self.summary.n_scored == self.summary.n_questions and not self.summary.budget_exhausted
        )


async def _mint_eval_run(ctx: SweepContext, row: SweepRow, subset: str | None) -> uuid.UUID:
    """Pre-create the eval run row so the sweep can record it in state *before* any
    question runs — a crash at any later point leaves a resumable id behind. The
    runner is then always entered through its resume path."""
    run_id = uuid.uuid4()
    await ctx.pool.execute(
        "INSERT INTO app.eval_runs (id, suite_hash, model, git_sha, summary) "
        "VALUES ($1, $2, $3, $4, $5::jsonb)",
        run_id,
        ctx.suite.suite_hash,
        row.model,
        git_sha(),  # the column feeds the eval dashboard's run list
        canonical_dumps(
            {
                "track": ctx.matrix.track,
                "subset": subset,
                "status": "running",
                "sweep": True,
            }
        ),
    )
    return run_id


def _resolve_resume(
    ctx: SweepContext, row: SweepRow, state: dict[str, dict[str, str]], fresh: bool
) -> uuid.UUID | None:
    if fresh:
        return None
    entry = state.get(row.model)
    if entry is None:
        return None
    if entry.get("suite_hash") != ctx.suite.suite_hash or entry.get("track") != ctx.matrix.track:
        return None  # the suite (or shape) moved under the half-run; start over
    return uuid.UUID(entry["eval_run_id"])


async def run_row(
    ctx: SweepContext,
    row: SweepRow,
    *,
    budget_override: Decimal | None = None,
    resume_run_id: uuid.UUID | None = None,
    assume_yes: bool = False,
    judge_model: str | None = None,
    no_judge: bool = False,
    subset: Literal["gate", "smoke"] | None = None,
    fresh: bool = False,
) -> RowOutcome:
    """Run one model row end-to-end: eval run → trace aggregates → results JSON.

    Dry passes (``subset`` set) run the same pipeline but never write to
    ``results_dir`` or the state file — the committed artifacts are full-suite only.
    """
    effective_row = (
        row if budget_override is None else SweepRow(model=row.model, budget_usd=budget_override)
    )
    state_file = ctx.state_file
    state = load_state(state_file) if state_file is not None else {}

    resumed_from_state = False
    if resume_run_id is None and subset is None:
        resume_run_id = _resolve_resume(ctx, row, state, fresh)
        if resume_run_id is not None:
            resumed_from_state = True
            print(f"[{row.model}] resuming eval run {resume_run_id} from sweep state")
    if resume_run_id is None:
        resume_run_id = await _mint_eval_run(ctx, row, subset)
        if state_file is not None and subset is None:
            state[row.model] = {
                "eval_run_id": str(resume_run_id),
                "suite_hash": ctx.suite.suite_hash,
                "track": ctx.matrix.track,
                "started_at": datetime.now(UTC).isoformat(timespec="seconds"),
            }
            save_state(state_file, state)

    def config_for(run_id: uuid.UUID) -> RunnerConfig:
        return RunnerConfig(
            suite=ctx.suite,
            model=row.model,
            track=ctx.matrix.track,
            subset=subset,
            budget_usd=effective_row.runner_budget,
            assume_yes=assume_yes,
            judge_model=None if no_judge else (judge_model or ctx.matrix.judge_model),
            concurrency=ctx.concurrency,
            out_dir=ctx.out_dir,
            data_dir=ctx.settings.data_path,
            resume_run_id=run_id,
        )

    try:
        summary = await ctx.runner.run(config_for(resume_run_id))
    except ValueError:
        if not resumed_from_state:
            raise
        # The state file outlived its eval run (reset DB, hand-deleted rows).
        # An explicit --resume stays a loud error; sweep state self-heals.
        print(f"[{row.model}] stale sweep state — eval run gone; starting fresh")
        if state_file is not None:
            state = load_state(state_file)
            state.pop(row.model, None)
            save_state(state_file, state)
        resume_run_id = await _mint_eval_run(ctx, row, subset)
        if state_file is not None and subset is None:
            state[row.model] = {
                "eval_run_id": str(resume_run_id),
                "suite_hash": ctx.suite.suite_hash,
                "track": ctx.matrix.track,
                "started_at": datetime.now(UTC).isoformat(timespec="seconds"),
            }
            save_state(state_file, state)
        summary = await ctx.runner.run(config_for(resume_run_id))

    if subset is not None:
        return RowOutcome(row=effective_row, summary=summary, doc=None, path=None)

    metrics = await collect_run_metrics(ctx.pool, summary.eval_run_id)
    doc = build_results_doc(
        summary.as_dict(),
        metrics,
        registry=ctx.registry,
        row=effective_row,
        suite_name=ctx.suite.name,
        settings=ctx.settings,
        concurrency=ctx.concurrency,
    )
    path = write_results_doc(ctx.results_dir, doc)

    if state_file is not None:
        state = load_state(state_file)
        if doc["complete"]:
            state.pop(row.model, None)
        else:
            entry = state.setdefault(row.model, {})
            entry.update(
                eval_run_id=str(summary.eval_run_id),
                suite_hash=ctx.suite.suite_hash,
                track=ctx.matrix.track,
            )
        save_state(state_file, state)

    return RowOutcome(row=effective_row, summary=summary, doc=doc, path=path)
