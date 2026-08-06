"""T3 — LLM-as-judge (BUILD_PLAN §5.1): rubric-scored prose quality, pinned prompts.

The judge grades faithfulness-to-citations, clarity, and appropriate hedging on a 1-5
rubric (``evals/judges/rubric_v1.md``, content-hashed like agent prompts); the judge
model, rubric hash, and per-question rationale are recorded so judged results pin to
the exact judge version. One forced ``grade`` tool call per answer — the judge is a
traced, metered run like every other LLM call in the repo (invariant 6).

The judge never sees the expected answer (that would bias a craft grade into an
accuracy re-check — T1 owns accuracy), and cited clause texts are fetched from the
chunk store by the *harness*, so the judge grades against exactly what the agent cited.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Any

import asyncpg
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from backline.core.costmeter import CostMeter
from backline.core.trace import Tracer
from backline.providers.base import CompletionRequest, Message, Provider, ToolSpec
from backline.providers.registry import ModelRegistry

JUDGES_DIR = Path(__file__).resolve().parent / "judges"
RUBRIC_NAME = "rubric_v1"

_CITATION_REF = re.compile(r"FBR-(?P<kind>[CA])-(?P<id>\d{5})\s+(?P<clause>§[A-Z0-9]+)")


@dataclass(frozen=True)
class JudgePrompt:
    name: str
    text: str
    sha256: str

    @property
    def short_hash(self) -> str:
        return self.sha256[:12]


@cache
def load_rubric(name: str = RUBRIC_NAME) -> JudgePrompt:
    path = JUDGES_DIR / f"{name}.md"
    raw = path.read_bytes()
    return JudgePrompt(
        name=name, text=raw.decode("utf-8").strip(), sha256=hashlib.sha256(raw).hexdigest()
    )


class GradeArgs(BaseModel):
    model_config = ConfigDict(frozen=True)

    faithfulness: int = Field(ge=1, le=5)
    clarity: int = Field(ge=1, le=5)
    hedging: int = Field(ge=1, le=5)
    rationale: str = Field(min_length=1, max_length=2000)


_GRADE_TOOL = ToolSpec(
    name="grade",
    description="Deliver your rubric grades. Call exactly once.",
    input_schema=GradeArgs.model_json_schema(),
)


@dataclass(frozen=True)
class JudgeVerdict:
    score: float  # mean of the three dimensions / 5, in 0..1
    grades: dict[str, int]
    rationale: str
    judge_model: str
    rubric_hash: str
    cost_usd: Any  # Decimal
    error: str | None = None


@dataclass(frozen=True)
class JudgeFailure:
    error: str
    judge_model: str
    rubric_hash: str
    cost_usd: Any


async def fetch_cited_clauses(
    pool: asyncpg.Pool, citations: tuple[str, ...], *, limit: int = 6
) -> list[tuple[str, str]]:
    """(ref, verbatim clause text) for each structural citation, from the chunk store."""
    fetched: list[tuple[str, str]] = []
    for ref in citations[:limit]:
        match = _CITATION_REF.search(ref)
        if match is None:
            continue
        rows = await pool.fetch(
            "SELECT content FROM rag.contract_chunks "
            "WHERE contract_id = $1 AND clause_no = $2 ORDER BY part",
            int(match.group("id")),
            match.group("clause"),
        )
        if rows:
            fetched.append((ref, "\n".join(row["content"] for row in rows)))
    return fetched


def _judge_user_message(
    question_prompt: str, answer_text: str, clauses: list[tuple[str, str]]
) -> str:
    parts = [f"<question>\n{question_prompt}\n</question>", ""]
    parts.append(f"<answer>\n{answer_text}\n</answer>")
    if clauses:
        parts.append("")
        parts.append("Cited clause texts (verbatim from the document store):")
        for ref, text in clauses:
            parts.append(f'<cited_clause ref="{ref}">\n{text}\n</cited_clause>')
    else:
        parts.append("")
        parts.append("(The answer cited no clauses; grade faithfulness accordingly.)")
    return "\n".join(parts)


@dataclass
class Judge:
    """Assembled like the router: provider + registry + tracer, one call per answer."""

    providers: dict[str, Provider]
    registry: ModelRegistry
    tracer: Tracer
    model: str
    rubric: JudgePrompt = field(default_factory=load_rubric)

    async def grade(
        self,
        *,
        question_id: str,
        question_prompt: str,
        answer_text: str,
        clauses: list[tuple[str, str]],
    ) -> JudgeVerdict:
        info = self.registry.get(self.model)
        provider = self.providers.get(info.provider)
        if provider is None:
            raise RuntimeError(
                f"judge model {self.model!r} needs provider {info.provider!r}, "
                f"but only {sorted(self.providers)} are configured"
            )
        costmeter = CostMeter(self.registry)
        error: str | None = None
        grades: GradeArgs | None = None
        async with self.tracer.run(
            agent="judge",
            meta={
                "model": self.model,
                "rubric": self.rubric.name,
                "rubric_sha256": self.rubric.short_hash,
                "question_id": question_id,
            },
        ) as run:
            async with run.span("llm_call", f"llm:{self.model}") as span:
                result = await provider.complete(
                    CompletionRequest(
                        model=self.model,
                        system=self.rubric.text,
                        messages=[
                            Message(
                                role="user",
                                content=_judge_user_message(question_prompt, answer_text, clauses),
                            )
                        ],
                        tools=[_GRADE_TOOL],
                        tool_choice="grade",
                        max_tokens=700,
                    )
                )
                cost = costmeter.add(self.model, result.usage)
                span.attrs.update(
                    {
                        "gen_ai.request.model": self.model,
                        "gen_ai.usage.input_tokens": result.usage.input_tokens,
                        "gen_ai.usage.output_tokens": result.usage.output_tokens,
                        "cost_usd": cost,
                        "stop_reason": result.stop_reason,
                    }
                )
            call = next((c for c in result.tool_calls if c.name == "grade"), None)
            if call is None:
                error = "judge returned no grade tool call"
            else:
                try:
                    grades = GradeArgs.model_validate(call.arguments)
                except ValidationError as exc:
                    first = exc.errors(include_url=False)[0]
                    error = f"grade arguments failed validation: {first['msg']}"
            run.meta["judged"] = error is None
            run.set_result(status="completed", cost_usd=costmeter.total_usd)

        if grades is None:
            return JudgeVerdict(
                score=0.0,
                grades={},
                rationale="",
                judge_model=self.model,
                rubric_hash=self.rubric.short_hash,
                cost_usd=costmeter.total_usd,
                error=error or "judge failed",
            )
        mean = (grades.faithfulness + grades.clarity + grades.hedging) / 3
        return JudgeVerdict(
            score=round(mean / 5, 4),
            grades={
                "faithfulness": grades.faithfulness,
                "clarity": grades.clarity,
                "hedging": grades.hedging,
            },
            rationale=grades.rationale,
            judge_model=self.model,
            rubric_hash=self.rubric.short_hash,
            cost_usd=costmeter.total_usd,
            error=None,
        )
