"""T3 judge tests on MockProvider: forced grade call, malformed-output degradation,
rubric pinning, and the traced/metered judge run."""

from decimal import Decimal

from backline.core.trace import InMemorySink, Tracer
from backline.providers.base import ToolCall
from backline.providers.mock import MockProvider, MockTurn
from backline.providers.registry import ModelRegistry
from evals.judge import Judge, load_rubric


def _judge(provider: MockProvider) -> tuple[Judge, InMemorySink]:
    sink = InMemorySink()
    return (
        Judge(
            providers={"mock": provider},
            registry=ModelRegistry.load(),
            tracer=Tracer([sink]),
            model="mock-sonnet",
        ),
        sink,
    )


async def test_judge_parses_grades_and_records_provenance() -> None:
    provider = MockProvider(
        [
            MockTurn(
                tool_calls=[
                    ToolCall(
                        id="g1",
                        name="grade",
                        arguments={
                            "faithfulness": 5,
                            "clarity": 4,
                            "hedging": 3,
                            "rationale": "Claims trace to the quoted clause.",
                        },
                    )
                ],
                match="<answer>",  # the judge saw the framed answer
            )
        ]
    )
    judge, sink = _judge(provider)
    verdict = await judge.grade(
        question_id="contract_terms-001",
        question_prompt="What rate applies?",
        answer_text="The rate is 22% (FBR-C-00001 §3).\nANSWER: 22%",
        clauses=[("FBR-C-00001 §3", "ROYALTIES. ... 22% ...")],
    )
    assert verdict.error is None
    assert verdict.grades == {"faithfulness": 5, "clarity": 4, "hedging": 3}
    assert verdict.score == round((5 + 4 + 3) / 3 / 5, 4)
    assert verdict.judge_model == "mock-sonnet"
    assert verdict.rubric_hash == load_rubric().short_hash
    assert verdict.cost_usd > Decimal("0")
    # The judge is a traced, metered run (invariant 6).
    run = sink.runs[0]
    assert run.agent == "judge"
    assert run.meta["rubric_sha256"] == load_rubric().short_hash
    assert run.cost_usd == verdict.cost_usd
    llm_spans = [s for s in sink.spans if s.kind == "llm_call"]
    assert len(llm_spans) == 1
    # The rubric is pinned as the system prompt, verbatim.
    assert provider.calls[0].system == load_rubric().text
    assert provider.calls[0].tool_choice == "grade"


async def test_judge_degrades_on_missing_tool_call() -> None:
    provider = MockProvider([MockTurn(text="I think it deserves a 4.")])
    judge, _sink = _judge(provider)
    verdict = await judge.grade(question_id="q", question_prompt="p", answer_text="a", clauses=[])
    assert verdict.error == "judge returned no grade tool call"
    assert verdict.score == 0.0 and verdict.grades == {}


async def test_judge_degrades_on_invalid_arguments() -> None:
    provider = MockProvider(
        [
            MockTurn(
                tool_calls=[
                    ToolCall(
                        id="g1",
                        name="grade",
                        arguments={"faithfulness": 9, "clarity": 4, "hedging": 3, "rationale": "x"},
                    )
                ]
            )
        ]
    )
    judge, _sink = _judge(provider)
    verdict = await judge.grade(question_id="q", question_prompt="p", answer_text="a", clauses=[])
    assert verdict.error is not None and "validation" in verdict.error
    assert verdict.score == 0.0


async def test_judge_never_sees_expected_answer_frame() -> None:
    """The judge input carries question/answer/cited clauses — no expected value."""
    provider = MockProvider(
        [
            MockTurn(
                tool_calls=[
                    ToolCall(
                        id="g1",
                        name="grade",
                        arguments={
                            "faithfulness": 5,
                            "clarity": 5,
                            "hedging": 5,
                            "rationale": "clean",
                        },
                    )
                ]
            )
        ]
    )
    judge, _sink = _judge(provider)
    await judge.grade(
        question_id="q",
        question_prompt="What is X's payable?",
        answer_text="ANSWER: $10.00",
        clauses=[],
    )
    body = provider.calls[0].messages[0].content
    assert "<question>" in body and "<answer>" in body
    assert "expected" not in body.casefold()
