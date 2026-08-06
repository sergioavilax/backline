"""The eval runner (BUILD_PLAN §5): async, per-model, budget-guarded, resumable.

One invocation = one ``app.eval_runs`` row keyed ``(suite_hash, model, git_sha)``,
one ``app.eval_results`` row per (question, tier), and a JSON artifact directory
(``results.jsonl`` streamed per question + ``summary.json``). Questions run
concurrently under a semaphore; every agent/baseline/judge call is traced through the
normal sinks, so eval traces are inspectable exactly like production runs.

Budget guard (§5.4): the harness projects token spend from suite stats *before*
running and refuses to start when the projection exceeds ``--budget`` without
``--yes``; during the run the budget is a hard stop — remaining questions are skipped
(recorded as such) and the run is resumable with ``--resume <eval_run_id>``, which
skips every question that already has scored rows.

Scoring: T1 always (per tiers), T2 walks the in-memory span tree of exactly this
question's run, T3 judges platform answers when a judge model is configured.
A question's score is the *minimum* of its tier scores — a right number produced by a
forbidden process (or a beautiful answer with a wrong number) does not pass.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import subprocess
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

import asyncpg

from backline.agents.configs import build_agent
from backline.config import Settings, get_settings
from backline.core.runtime import AgentRuntime
from backline.core.trace import InMemorySink, Tracer, TraceSink
from backline.jsonutil import canonical_dumps
from backline.providers.base import Provider
from backline.providers.registry import ModelRegistry
from backline.tools.context import ToolContext
from evals.baselines import BaselineAnswer, CorpusIndex, answer_b0, answer_b1
from evals.judge import Judge, JudgeVerdict, fetch_cited_clauses, load_rubric
from evals.scoring import AnswerOutcome, TierScore, score_t1
from evals.trace_asserts import score_t2
from evals.types import Question, Suite, Track, load_answer_key

ProviderFactory = Callable[[Question], Mapping[str, Provider]]

# Per-question token estimates for the §5.4 pre-run projection and the in-flight
# budget reservations. These are whole-loop totals, not one request: an agent resends
# its entire growing context every iteration, so input ≈ Σ per-iteration context —
# the original single-round-trip guesses (counsel 9000/1200, analyst 5000/900,
# reconciler 16000/2500, judge 2500/400) projected $6.86 for the full suite where run
# 2b9f39fb's meter recorded $16.74 at identical per-token prices, a 2.4x undershoot
# that was almost entirely the reconciler (real mean $0.45/question vs $0.0855
# guessed). Calibrated from that run's per-question costs: judge-subtracted per-agent
# means, converted to tokens at the run's 3/15 metering (suite total at these numbers:
# $16.90 vs $16.74 metered). The reconciler mean is cap-censored — 6 of its 22 runs
# hit the per-run budget cap — so it floors reconciler-heavy projections; per-run
# caps bound each question's actual spend regardless (D-019).
_PROJECTION: dict[str, tuple[int, int]] = {
    "counsel": (14_000, 1_800),
    "analyst": (4_500, 750),
    "reconciler": (87_000, 12_700),
    "b0": (0, 800),  # + pack budget, added at projection time
    "b1": (4_500, 800),
    "judge": (3_000, 450),
}


class BudgetRefused(RuntimeError):
    """Projection exceeds the budget and --yes was not given."""


@dataclass(frozen=True)
class RunnerConfig:
    suite: Suite
    model: str
    track: Track = "platform"
    subset: Literal["gate", "smoke"] | None = None
    # Restrict the run to these categories (post-subset) — targeted re-runs after a
    # harness fix, priced per question instead of per suite. A filtered summary is
    # not gate-comparable (missing categories read as regressions); don't --gate it.
    categories: tuple[str, ...] | None = None
    budget_usd: Decimal = Decimal("5.00")
    assume_yes: bool = False
    judge_model: str | None = None  # platform track only; None = skip T3
    utility_model: str | None = None
    concurrency: int = 4
    out_dir: Path = Path("data/evals")
    data_dir: Path = Path("data")
    resume_run_id: uuid.UUID | None = None
    pack_tokens: int = 24_000
    b1_top_k: int = 12


@dataclass
class QuestionResult:
    question: Question
    tiers: dict[str, TierScore]
    cost_usd: Decimal
    latency_ms: int
    run_id: uuid.UUID | None
    answer_text: str
    error: str | None = None

    @property
    def score(self) -> float:
        return min((tier.score for tier in self.tiers.values()), default=0.0)


@dataclass
class RunSummary:
    eval_run_id: uuid.UUID
    suite_hash: str
    model: str
    track: str
    subset: str | None
    git_sha: str | None
    categories: dict[str, dict[str, Any]]
    t2_violations: int
    n_questions: int
    n_scored: int
    n_skipped_budget: int
    total_cost_usd: Decimal
    budget_usd: Decimal
    budget_exhausted: bool
    judge: dict[str, str] | None
    latency_ms_p50: int
    latency_ms_p95: int
    out_dir: Path

    def as_dict(self) -> dict[str, Any]:
        return {
            "eval_run_id": str(self.eval_run_id),
            "suite_hash": self.suite_hash,
            "model": self.model,
            "track": self.track,
            "subset": self.subset,
            "git_sha": self.git_sha,
            "categories": self.categories,
            "t2_violations": self.t2_violations,
            "n_questions": self.n_questions,
            "n_scored": self.n_scored,
            "n_skipped_budget": self.n_skipped_budget,
            "total_cost_usd": str(self.total_cost_usd),
            "budget_usd": str(self.budget_usd),
            "budget_exhausted": self.budget_exhausted,
            "judge": self.judge,
            "latency_ms_p50": self.latency_ms_p50,
            "latency_ms_p95": self.latency_ms_p95,
        }


def git_sha() -> str | None:
    sha = os.environ.get("GITHUB_SHA")
    if sha:
        return sha[:12]
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short=12", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except OSError:
        return None
    return out.stdout.strip() or None


def project_question_cost(
    question: Question, config: RunnerConfig, registry: ModelRegistry
) -> Decimal:
    """Projected spend for one question (agent loop + judge when T3 applies).

    Doubles as the reservation the runner holds while the question is in flight, so
    the budget gate sees committed spend — not just costs that have already landed.
    """
    info = registry.get(config.model)
    per_mtok = Decimal(1_000_000)
    if config.track == "platform":
        tokens_in, tokens_out = _PROJECTION[question.agent]
    else:
        tokens_in, tokens_out = _PROJECTION[config.track]
        if config.track == "b0":
            tokens_in += config.pack_tokens
    total = (info.usd_per_mtok_in * tokens_in + info.usd_per_mtok_out * tokens_out) / per_mtok
    if config.track == "platform" and config.judge_model is not None and "t3" in question.tiers:
        judge_info = registry.get(config.judge_model)
        j_in, j_out = _PROJECTION["judge"]
        total += (
            judge_info.usd_per_mtok_in * j_in + judge_info.usd_per_mtok_out * j_out
        ) / per_mtok
    return total


def project_cost(
    questions: Sequence[Question], config: RunnerConfig, registry: ModelRegistry
) -> Decimal:
    total = sum(
        (project_question_cost(question, config, registry) for question in questions),
        Decimal("0"),
    )
    return total.quantize(Decimal("0.01"))


class EvalRunner:
    def __init__(
        self,
        *,
        pool: asyncpg.Pool,
        providers: Mapping[str, Provider] | None = None,
        registry: ModelRegistry,
        settings: Settings | None = None,
        embedder: Any = None,
        reranker: Any = None,
        extra_sinks: Sequence[TraceSink] = (),
        provider_factory: ProviderFactory | None = None,
        judge_provider_factory: ProviderFactory | None = None,
    ) -> None:
        if providers is None and provider_factory is None:
            raise ValueError("EvalRunner needs providers or a provider_factory")
        self._pool = pool
        self._providers = dict(providers or {})
        self._registry = registry
        self._settings = settings or get_settings()
        self._embedder = embedder
        self._reranker = reranker
        self._extra_sinks = list(extra_sinks)
        self._provider_factory = provider_factory
        self._judge_provider_factory = judge_provider_factory

    # ── plumbing ─────────────────────────────────────────────────────────────

    def _providers_for(self, question: Question) -> dict[str, Provider]:
        if self._provider_factory is not None:
            return dict(self._provider_factory(question))
        return self._providers

    def _judge_providers_for(self, question: Question) -> dict[str, Provider]:
        if self._judge_provider_factory is not None:
            return dict(self._judge_provider_factory(question))
        return self._providers_for(question)

    async def _create_or_resume_run(
        self, config: RunnerConfig, sha: str | None
    ) -> tuple[uuid.UUID, set[str]]:
        if config.resume_run_id is not None:
            row = await self._pool.fetchrow(
                "SELECT suite_hash, model FROM app.eval_runs WHERE id = $1",
                config.resume_run_id,
            )
            if row is None:
                raise ValueError(f"no eval run {config.resume_run_id} to resume")
            if row["suite_hash"] != config.suite.suite_hash or row["model"] != config.model:
                raise ValueError(
                    f"resume mismatch: run {config.resume_run_id} is "
                    f"({row['suite_hash']}, {row['model']}), asked for "
                    f"({config.suite.suite_hash}, {config.model})"
                )
            done_rows = await self._pool.fetch(
                "SELECT DISTINCT question_id FROM app.eval_results WHERE eval_run_id = $1",
                config.resume_run_id,
            )
            return config.resume_run_id, {r["question_id"] for r in done_rows}
        run_id = uuid.uuid4()
        await self._pool.execute(
            "INSERT INTO app.eval_runs (id, suite_hash, model, git_sha, summary) "
            "VALUES ($1, $2, $3, $4, $5::jsonb)",
            run_id,
            config.suite.suite_hash,
            config.model,
            sha,
            canonical_dumps({"track": config.track, "subset": config.subset, "status": "running"}),
        )
        return run_id, set()

    async def _persist_question(
        self, eval_run_id: uuid.UUID, result: QuestionResult, artifact: Path
    ) -> None:
        base_detail = {
            "agent": result.question.agent,
            "category": result.question.category,
            "run_id": str(result.run_id) if result.run_id else None,
            "cost_usd": str(result.cost_usd),
            "latency_ms": result.latency_ms,
        }
        if result.error is not None:
            base_detail["error"] = result.error
        rows = [
            (
                eval_run_id,
                result.question.id,
                tier,
                Decimal(str(round(score.score, 4))),
                score.passed,
                canonical_dumps({**base_detail, **score.detail}),
            )
            for tier, score in result.tiers.items()
        ]
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.executemany(
                "INSERT INTO app.eval_results "
                "(eval_run_id, question_id, tier, score, passed, detail) "
                "VALUES ($1, $2, $3, $4, $5, $6::jsonb)",
                rows,
            )
        line = canonical_dumps(
            {
                "question_id": result.question.id,
                "category": result.question.category,
                "score": round(result.score, 4),
                "tiers": {
                    tier: {"score": round(s.score, 4), "passed": s.passed, **s.detail}
                    for tier, s in result.tiers.items()
                },
                **base_detail,
                "answer_text": result.answer_text[:2000],
            }
        )
        with artifact.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    # ── per-question execution ───────────────────────────────────────────────

    async def _run_platform_question(
        self, config: RunnerConfig, question: Question, tracer: Tracer
    ) -> tuple[AnswerOutcome, Decimal, uuid.UUID | None, int]:
        providers = self._providers_for(question)
        agent = build_agent(
            question.agent,
            ToolContext(
                pool=self._pool,
                settings=self._settings,
                embedder=self._embedder,
                reranker=self._reranker,
            ),
            model=config.model,
            utility_model=config.utility_model,
        )
        runtime = AgentRuntime(providers=providers, registry=self._registry, tracer=tracer)
        started = time.perf_counter()
        result = await runtime.run(agent, question.prompt)
        latency = int((time.perf_counter() - started) * 1000)
        final = result.final
        outcome = AnswerOutcome(
            text=final.answer if final is not None else "",
            abstained=final.abstained if final is not None else False,
            citations=tuple(c.ref for c in final.citations) if final is not None else (),
            batch_id=getattr(final, "batch_id", None),
            status=result.status,
        )
        return outcome, result.cost_usd, result.run_id, latency

    async def _run_baseline_question(
        self, config: RunnerConfig, question: Question, tracer: Tracer, index: CorpusIndex | None
    ) -> BaselineAnswer:
        providers = self._providers_for(question)
        if config.track == "b0":
            assert index is not None
            return await answer_b0(
                providers=providers,
                registry=self._registry,
                tracer=tracer,
                model=config.model,
                question=question,
                index=index,
                pack_tokens=config.pack_tokens,
            )
        return await answer_b1(
            providers=providers,
            registry=self._registry,
            tracer=tracer,
            pool=self._pool,
            model=config.model,
            question=question,
            top_k=config.b1_top_k,
            embedder=self._embedder,
        )

    async def _score_question(
        self, config: RunnerConfig, question: Question, index: CorpusIndex | None
    ) -> QuestionResult:
        mem = InMemorySink()
        tracer = Tracer([mem, *self._extra_sinks])
        tiers: dict[str, TierScore] = {}
        judge_cost = Decimal("0")
        try:
            if config.track == "platform":
                outcome, cost, run_id, latency = await self._run_platform_question(
                    config, question, tracer
                )
            else:
                baseline = await self._run_baseline_question(config, question, tracer, index)
                outcome, cost, run_id, latency = (
                    baseline.outcome,
                    baseline.cost_usd,
                    baseline.run_id,
                    baseline.latency_ms,
                )
        except Exception as exc:  # a broken question must not kill the suite
            failure = TierScore(0.0, False, {"failure": "harness_error", "error": str(exc)})
            return QuestionResult(
                question=question,
                tiers={tier: failure for tier in question.tiers} or {"t1": failure},
                cost_usd=Decimal("0"),
                latency_ms=0,
                run_id=None,
                answer_text="",
                error=f"{type(exc).__name__}: {exc}",
            )

        if "t1" in question.tiers:
            tiers["t1"] = score_t1(question, outcome)
        if "t2" in question.tiers:
            if config.track == "platform":
                spans = [s for s in mem.spans if run_id is not None and s.run_id == run_id]
                t2_score, t2_passed, t2_detail = score_t2(question.t2_checks, spans, outcome)
                tiers["t2"] = TierScore(t2_score, t2_passed, t2_detail)
            else:
                # No tool trace exists to assert over; recorded, never counted.
                tiers["t2"] = TierScore(1.0, True, {"not_applicable": config.track})
        if "t3" in question.tiers and config.track == "platform" and config.judge_model is not None:
            verdict = await self._judge(config, question, outcome, tracer)
            judge_cost = verdict.cost_usd
            tiers["t3"] = TierScore(
                verdict.score,
                verdict.error is None and verdict.score >= 0.6,
                {
                    "grades": verdict.grades,
                    "rationale": verdict.rationale,
                    "judge_model": verdict.judge_model,
                    "rubric_sha256": verdict.rubric_hash,
                    **({"error": verdict.error} if verdict.error else {}),
                },
            )
        return QuestionResult(
            question=question,
            tiers=tiers,
            cost_usd=cost + judge_cost,
            latency_ms=latency,
            run_id=run_id,
            answer_text=outcome.text,
        )

    async def _judge(
        self,
        config: RunnerConfig,
        question: Question,
        outcome: AnswerOutcome,
        tracer: Tracer,
    ) -> JudgeVerdict:
        assert config.judge_model is not None
        clauses = await fetch_cited_clauses(self._pool, outcome.citations)
        judge = Judge(
            providers=self._judge_providers_for(question),
            registry=self._registry,
            tracer=tracer,
            model=config.judge_model,
        )
        return await judge.grade(
            question_id=question.id,
            question_prompt=question.prompt,
            answer_text=outcome.text,
            clauses=clauses,
        )

    # ── the run ──────────────────────────────────────────────────────────────

    async def run(self, config: RunnerConfig) -> RunSummary:
        questions = config.suite.subset(config.subset)
        if config.categories is not None:
            wanted = set(config.categories)
            unknown = wanted - {q.category for q in config.suite.questions}
            if unknown:
                raise ValueError(f"unknown categories: {sorted(unknown)}")
            questions = [q for q in questions if q.category in wanted]
            if not questions:
                raise ValueError(f"no questions in categories {sorted(wanted)} for this subset")
        if config.track != "platform":
            # T3 is platform-only; baselines answer every question all the same.
            config = dataclasses.replace(config, judge_model=None)

        projection = project_cost(questions, config, self._registry)
        info = self._registry.get(config.model)
        price = f"${info.usd_per_mtok_in}/${info.usd_per_mtok_out} per Mtok"
        if info.price_note:
            price = f"{price} — {info.price_note}"
        print(
            f"eval run: {len(questions)} questions · track={config.track} · "
            f"model={config.model} @ {price} · "
            f"projected ≈ ${projection} · budget ${config.budget_usd}"
        )
        if projection > config.budget_usd and not config.assume_yes:
            raise BudgetRefused(
                f"projected spend ${projection} exceeds budget ${config.budget_usd} — "
                f"raise --budget or pass --yes to proceed under the hard cap"
            )

        await load_answer_key(self._pool, config.suite)
        sha = git_sha()
        eval_run_id, done = await self._create_or_resume_run(config, sha)
        out_dir = config.out_dir / str(eval_run_id)
        out_dir.mkdir(parents=True, exist_ok=True)
        artifact = out_dir / "results.jsonl"

        index = CorpusIndex.build(config.data_dir) if config.track == "b0" else None
        pending = [q for q in questions if q.id not in done]

        spent_lock = asyncio.Lock()
        spent = Decimal("0")
        reserved = Decimal("0")
        skipped: list[str] = []
        stop_announced = False
        semaphore = asyncio.Semaphore(config.concurrency)

        async def one(question: Question) -> None:
            nonlocal spent, reserved, stop_announced
            projected = project_question_cost(question, config, self._registry)
            async with semaphore:
                async with spent_lock:
                    # The gate reads committed spend: landed cost + a projected
                    # reservation per in-flight question. Landed cost alone let run
                    # 2b9f39fb finish all 133 questions past its cap — slow expensive
                    # questions held their cost invisibly in flight (p95 115s vs p50
                    # 15s) while cheap ones sailed through the check.
                    if spent + reserved >= config.budget_usd:
                        skipped.append(question.id)
                        if not stop_announced:
                            stop_announced = True
                            print(
                                f"budget hard stop: ${spent} landed + ${reserved} "
                                f"reserved for in-flight questions ≥ "
                                f"${config.budget_usd} — skipping the rest "
                                f"(resumable with --resume)"
                            )
                        return
                    reserved += projected
                result = await self._score_question(config, question, index)
                async with spent_lock:
                    reserved -= projected
                    spent += result.cost_usd
                await self._persist_question(eval_run_id, result, artifact)

        await asyncio.gather(*(one(question) for question in pending))
        if spent > config.budget_usd:
            print(
                f"warning: metered spend ${spent} overshot the ${config.budget_usd} "
                f"budget — in-flight questions at the stop boundary cost more than "
                f"their reservations"
            )

        summary = await self._summarize(
            config, eval_run_id, sha, questions, skipped, spent, out_dir
        )
        (out_dir / "summary.json").write_text(
            canonical_dumps(summary.as_dict()) + "\n", encoding="utf-8"
        )
        return summary

    async def _summarize(
        self,
        config: RunnerConfig,
        eval_run_id: uuid.UUID,
        sha: str | None,
        questions: Sequence[Question],
        skipped: Sequence[str],
        spent_this_call: Decimal,
        out_dir: Path,
    ) -> RunSummary:
        rows = await self._pool.fetch(
            "SELECT question_id, tier, score, passed, detail FROM app.eval_results "
            "WHERE eval_run_id = $1",
            eval_run_id,
        )
        by_question: dict[str, dict[str, tuple[float, bool]]] = {}
        latencies: list[int] = []
        total_cost = Decimal("0")
        seen_cost: set[str] = set()
        for row in rows:
            tiers = by_question.setdefault(row["question_id"], {})
            tiers[row["tier"]] = (float(row["score"] or 0), bool(row["passed"]))
            if row["question_id"] not in seen_cost:
                seen_cost.add(row["question_id"])
                detail = json.loads(row["detail"])
                total_cost += Decimal(str(detail.get("cost_usd", "0")))
                latencies.append(int(detail.get("latency_ms", 0)))

        question_by_id = {q.id: q for q in questions}
        categories: dict[str, dict[str, Any]] = {}
        t2_violations = 0
        for question_id, tiers in by_question.items():
            question = question_by_id.get(question_id)
            if question is None:
                continue
            bucket = categories.setdefault(
                question.category,
                {"n": 0, "score_sum": 0.0, "tiers": {}},
            )
            bucket["n"] += 1
            question_score = min((score for score, _ in tiers.values()), default=0.0)
            bucket["score_sum"] += question_score
            for tier, (score, passed) in tiers.items():
                tier_bucket = bucket["tiers"].setdefault(tier, {"n": 0, "sum": 0.0})
                tier_bucket["n"] += 1
                tier_bucket["sum"] += score
                if tier == "t2" and not passed and config.track == "platform":
                    t2_violations += 1

        category_summary = {
            category: {
                "n": bucket["n"],
                "score": round(100.0 * bucket["score_sum"] / bucket["n"], 2),
                "tiers": {
                    tier: round(100.0 * tb["sum"] / tb["n"], 2)
                    for tier, tb in sorted(bucket["tiers"].items())
                },
            }
            for category, bucket in sorted(categories.items())
        }
        latencies.sort()

        def pct(p: float) -> int:
            if not latencies:
                return 0
            return latencies[min(len(latencies) - 1, int(p * len(latencies)))]

        judge_meta = (
            {"model": config.judge_model, "rubric_sha256": load_rubric().short_hash}
            if config.judge_model is not None
            else None
        )
        summary = RunSummary(
            eval_run_id=eval_run_id,
            suite_hash=config.suite.suite_hash,
            model=config.model,
            track=config.track,
            subset=config.subset,
            git_sha=sha,
            categories=category_summary,
            t2_violations=t2_violations,
            n_questions=len(questions),
            n_scored=len(by_question),
            n_skipped_budget=len(skipped),
            total_cost_usd=total_cost,
            budget_usd=config.budget_usd,
            budget_exhausted=bool(skipped),
            judge=judge_meta,
            latency_ms_p50=pct(0.50),
            latency_ms_p95=pct(0.95),
            out_dir=out_dir,
        )
        await self._pool.execute(
            "UPDATE app.eval_runs SET finished_at = now(), summary = $2::jsonb WHERE id = $1",
            eval_run_id,
            canonical_dumps(summary.as_dict()),
        )
        return summary
