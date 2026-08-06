"""Suite types (BUILD_PLAN §5.2): the question set every eval track answers.

A suite is a committed JSON artifact under ``evals/suites/`` — generated from the
answer key plus hand-authored hard cases — and is content-hashed: results are keyed
to ``(suite_hash, model, git_sha)``, so a changed suite can never be silently
compared against a stale baseline. ``load_suite`` verifies the stored hash against
the questions it loads; a mismatch is corruption, not a warning.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from backline.jsonutil import canonical_dumps

SUITES_DIR = Path(__file__).resolve().parent / "suites"

Category = Literal[
    "catalog_lookup",
    "contract_terms",
    "royalty_math",
    "recoupment_state",
    "cross_collateral",
    "sql_analytics",
    "reconciliation",
    "multi_step",
    "abstention",
    "adversarial",
]

CATEGORIES: tuple[Category, ...] = (
    "catalog_lookup",
    "contract_terms",
    "royalty_math",
    "recoupment_state",
    "cross_collateral",
    "sql_analytics",
    "reconciliation",
    "multi_step",
    "abstention",
    "adversarial",
)

# §5.2 targets — the generator fills each category to exactly this size
# (hand-authored cases count toward their category's total).
CATEGORY_TARGETS: dict[Category, int] = {
    "catalog_lookup": 15,
    "contract_terms": 20,
    "royalty_math": 25,
    "recoupment_state": 15,
    "cross_collateral": 8,
    "sql_analytics": 10,
    "reconciliation": 15,
    "multi_step": 12,
    "abstention": 10,
    "adversarial": 3,
}

AgentName = Literal["counsel", "analyst", "reconciler"]
Tier = Literal["t1", "t2", "t3"]
Track = Literal["platform", "b0", "b1"]

# How the T1 scorer reads ``expected`` (evals/scoring.py):
#   money    Decimal string, compared within ``tolerance`` (default ±$0.01)
#   count    exact integer
#   value    exact string (case-insensitive, whitespace-normalized)
#   percent  royalty points as a Decimal string ("30" = 30%); "0.30" answers normalize
#   set      list of strings, order-free exact match (semicolon-separated answers)
#   bool     YES / NO
#   period   "YYYY-MM"
#   abstain  the typed abstention flag must be set (no ANSWER line expected)
#   flags    reconciliation: {"flags": [{kind, source, line_id}], "borderline_line_ids":
#            [...]} scored as precision/recall/F1 against the registry
AnswerKind = Literal[
    "money", "count", "value", "percent", "set", "bool", "period", "abstain", "flags"
]


class Question(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    category: Category
    # Eval questions pin their agent — the suite measures agent competence, not
    # routing (the router has its own tests); see D-015.
    agent: AgentName
    tiers: list[Tier]
    prompt: str
    answer_kind: AnswerKind
    expected: Any = None
    tolerance: str | None = None  # Decimal string; money kinds only
    t2_checks: list[str] = Field(default_factory=list)
    in_gate: bool = False  # the budget-capped CI regression subset
    in_smoke: bool = False  # the keyless MockProvider plumbing subset
    source: Literal["generated", "hand"] = "generated"
    # Supporting facts for harnesses (smoke scripting, report drill-down): artist,
    # period, contract ids, reference SQL... — never shown to the agent under eval.
    meta: dict[str, Any] = Field(default_factory=dict)


class Suite(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    world_seed: int
    suite_hash: str
    questions: list[Question]

    def counts(self) -> dict[str, int]:
        by_category: dict[str, int] = {}
        for question in self.questions:
            by_category[question.category] = by_category.get(question.category, 0) + 1
        return dict(sorted(by_category.items()))

    def subset(self, name: Literal["gate", "smoke"] | None) -> list[Question]:
        if name is None:
            return list(self.questions)
        if name == "gate":
            return [q for q in self.questions if q.in_gate]
        return [q for q in self.questions if q.in_smoke]

    def by_id(self) -> dict[str, Question]:
        return {q.id: q for q in self.questions}


def suite_hash(questions: list[Question]) -> str:
    """Content hash over the questions exactly as serialized (order included)."""
    payload = canonical_dumps([q.model_dump(mode="json") for q in questions])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def dump_suite(suite: Suite) -> str:
    """Stable, reviewable serialization (indent 1, sorted keys) for the committed file."""
    import json

    return (
        json.dumps(
            {
                "name": suite.name,
                "world_seed": suite.world_seed,
                "suite_hash": suite.suite_hash,
                "counts": suite.counts(),
                "questions": [q.model_dump(mode="json") for q in suite.questions],
            },
            indent=1,
            sort_keys=True,
            ensure_ascii=False,
        )
        + "\n"
    )


def load_suite(path: Path | str = "core") -> Suite:
    """Load a suite by name (``evals/suites/{name}.json``) or path; verify its hash."""
    file = Path(path)
    if not file.suffix:
        file = SUITES_DIR / f"{file.name}.json"
    import json

    raw = json.loads(file.read_text(encoding="utf-8"))
    questions = [Question.model_validate(q) for q in raw["questions"]]
    computed = suite_hash(questions)
    if computed != raw["suite_hash"]:
        raise ValueError(
            f"suite {file.name}: stored suite_hash {raw['suite_hash']} != computed "
            f"{computed} — the file was edited without regenerating (run "
            f"`python -m evals generate`)"
        )
    return Suite(
        name=raw["name"],
        world_seed=raw["world_seed"],
        suite_hash=raw["suite_hash"],
        questions=questions,
    )


async def load_answer_key(pool: Any, suite: Suite) -> int:
    """Upsert the suite's expectations into ``truth.qa_answer_key`` (§3.3).

    The answer key lives in the ``truth`` schema like every other ground truth;
    agents can never read it (the SQL policy excludes ``truth.*`` at the parser).
    Idempotent — the runner calls it at start so any evaluated DB carries the key.
    """
    rows = [
        (
            q.id,
            canonical_dumps(
                {
                    "kind": q.answer_kind,
                    "value": q.expected,
                    "tiers": q.tiers,
                    "agent": q.agent,
                    "suite": suite.name,
                    "suite_hash": suite.suite_hash,
                }
            ),
            q.tolerance,
            q.category,
        )
        for q in suite.questions
    ]
    async with pool.acquire() as conn, conn.transaction():
        await conn.executemany(
            """
            INSERT INTO truth.qa_answer_key (question_id, answer, tolerance, category)
            VALUES ($1, $2::jsonb, $3::numeric, $4)
            ON CONFLICT (question_id)
            DO UPDATE SET answer = EXCLUDED.answer, tolerance = EXCLUDED.tolerance,
                          category = EXCLUDED.category
            """,
            rows,
        )
    return len(rows)
