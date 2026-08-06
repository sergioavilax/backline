"""Router on MockProvider: classify, threshold, fallbacks, tracing (keyless)."""

from decimal import Decimal

from backline.agents.promptfiles import load_prompt
from backline.agents.router import Router
from backline.core.trace import InMemorySink, Tracer
from backline.providers.base import ToolCall
from backline.providers.mock import MockProvider, MockTurn
from backline.providers.registry import ModelRegistry


def _router(provider: MockProvider, **kwargs: object) -> tuple[Router, InMemorySink]:
    sink = InMemorySink()
    router = Router(
        providers={"mock": provider},
        registry=ModelRegistry.load(),
        tracer=Tracer([sink]),
        model="mock-haiku",
        **kwargs,  # type: ignore[arg-type]
    )
    return router, sink


def _route_call(**arguments: object) -> ToolCall:
    return ToolCall(id="r1", name="route", arguments=dict(arguments))


async def test_confident_route_to_counsel() -> None:
    provider = MockProvider(
        [
            MockTurn(
                tool_calls=[
                    _route_call(
                        target="counsel",
                        confidence=0.92,
                        reason="asks what the contract says about sync",
                        artists=["Nova Reyes"],
                    )
                ],
                match="sync placements",
            )
        ]
    )
    router, sink = _router(provider)

    decision = await router.route("What's Nova Reyes' rate on sync placements?")

    assert decision.target == "counsel"
    assert decision.confidence == 0.92
    assert decision.artists == ["Nova Reyes"]

    # The classify call is forced onto the route tool and fully traced.
    request = provider.calls[0]
    assert request.tool_choice == "route"
    assert [t.name for t in request.tools] == ["route"]
    assert request.system == load_prompt("router").text
    run = sink.runs[0]
    assert run.agent == "router"
    assert run.meta["route_target"] == "counsel"
    assert run.meta["prompt_sha256"] == load_prompt("router").short_hash
    llm_spans = [s for s in sink.spans if s.kind == "llm_call"]
    assert len(llm_spans) == 1
    assert Decimal(str(llm_spans[0].attrs["cost_usd"])) > 0


async def test_low_confidence_downgrades_to_clarify() -> None:
    provider = MockProvider(
        [
            MockTurn(
                tool_calls=[
                    _route_call(
                        target="analyst",
                        confidence=0.35,
                        reason="might be a revenue question",
                        clarifying_question="Do you want reported revenue or payable royalties?",
                    )
                ]
            )
        ]
    )
    router, sink = _router(provider)

    decision = await router.route("what about the money for last month")

    assert decision.target == "clarify"
    assert decision.confidence == 0.35
    assert "analyst" in decision.reason  # the shadowed suggestion stays visible
    assert decision.clarifying_question == ("Do you want reported revenue or payable royalties?")
    assert sink.runs[0].meta["route_target"] == "clarify"


async def test_threshold_is_configurable() -> None:
    provider = MockProvider([MockTurn(tool_calls=[_route_call(target="analyst", confidence=0.35)])])
    router, _ = _router(provider, confidence_threshold=0.3)
    decision = await router.route("top tracks?")
    assert decision.target == "analyst"  # 0.35 clears a 0.3 bar


async def test_malformed_route_arguments_fall_back_to_clarify() -> None:
    provider = MockProvider([MockTurn(tool_calls=[_route_call(target="composer", confidence=0.9)])])
    router, _ = _router(provider)
    decision = await router.route("hello")
    assert decision.target == "clarify"
    assert decision.confidence == 0.0
    assert "validation" in decision.reason
    assert decision.clarifying_question  # canned question present


async def test_missing_tool_call_falls_back_to_clarify() -> None:
    provider = MockProvider([MockTurn(text="counsel, probably")])
    router, _ = _router(provider)
    decision = await router.route("hello")
    assert decision.target == "clarify"
    assert "no route tool call" in decision.reason
