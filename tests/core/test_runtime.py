"""AgentRuntime loop tests on MockProvider — the Phase 2 DoD paths, zero network."""

import asyncio
from decimal import Decimal

from pydantic import BaseModel

from backline.core.guardrails import RunLimits
from backline.core.memory import SessionMemory
from backline.core.runtime import AgentRuntime, AgentSpec, FinalAnswer, Tool
from backline.core.trace import InMemorySink, Tracer
from backline.providers.base import Message, ToolCall, Usage
from backline.providers.mock import MockProvider, MockTurn
from backline.providers.registry import ModelRegistry


class LookupParams(BaseModel):
    stage_name: str


async def lookup_artist(params: LookupParams) -> str:
    return f"artist {params.stage_name} has id 42"


def _lookup_tool() -> Tool[LookupParams]:
    return Tool(
        name="lookup_artist",
        description="Find an artist by stage name.",
        params=LookupParams,
        handler=lookup_artist,
    )


def _agent(**overrides: object) -> AgentSpec:
    defaults: dict[str, object] = {
        "name": "demo",
        "system_prompt": "You are a demo agent.",
        "model": "mock-sonnet",
        "tools": [_lookup_tool()],
        "limits": RunLimits(max_iterations=6, run_budget_usd=Decimal("0.50")),
    }
    defaults.update(overrides)
    return AgentSpec(**defaults)  # type: ignore[arg-type]


def _runtime(provider: MockProvider) -> tuple[AgentRuntime, InMemorySink]:
    sink = InMemorySink()
    runtime = AgentRuntime(
        providers={"mock": provider},
        registry=ModelRegistry.load(),
        tracer=Tracer([sink]),
    )
    return runtime, sink


def _call(id_: str = "c1", **arguments: object) -> ToolCall:
    return ToolCall(id=id_, name="lookup_artist", arguments=dict(arguments))


async def test_happy_path_tool_call_then_final_answer() -> None:
    provider = MockProvider(
        [
            MockTurn(
                text="Let me check.",
                tool_calls=[_call(stage_name="Nova Reyes")],
                match="Who is Nova Reyes?",
            ),
            MockTurn(text="Nova Reyes is artist 42.", match="artist Nova Reyes has id 42"),
        ]
    )
    runtime, sink = _runtime(provider)

    result = await runtime.run(_agent(), "Who is Nova Reyes?")

    assert result.status == "completed"
    assert result.final == FinalAnswer(answer="Nova Reyes is artist 42.")
    assert result.iterations == 2
    # 2 turns x (120 in x $3/M + 30 out x $15/M) = 2 x 0.000810, exact Decimal.
    assert result.cost_usd == Decimal("0.001620")

    # Provider saw the system prompt, the tool schema, and the tool result.
    first = provider.calls[0]
    assert first.system == "You are a demo agent."
    assert first.tools[0].name == "lookup_artist"
    assert first.tools[0].input_schema["properties"]["stage_name"]["type"] == "string"
    second = provider.calls[1]
    roles = [m.role for m in second.messages]
    assert roles == ["user", "assistant", "tool"]
    assert second.messages[2].tool_call_id == "c1"

    # Span tree: run → iteration → {llm_call, tool_call}; parentage asserted.
    spans = sink.spans
    kinds = [s.kind for s in spans]
    assert kinds == ["llm_call", "tool_call", "iteration", "llm_call", "iteration"]
    iterations = [s for s in spans if s.kind == "iteration"]
    assert all(s.parent_id is None for s in iterations)
    llm_spans = [s for s in spans if s.kind == "llm_call"]
    assert llm_spans[0].parent_id == iterations[0].id
    assert llm_spans[0].attrs["gen_ai.usage.input_tokens"] == 120
    assert llm_spans[0].attrs["cost_usd"] == Decimal("0.000810")
    assert llm_spans[0].attrs["stop_reason"] == "tool_use"
    tool_span = next(s for s in spans if s.kind == "tool_call")
    assert tool_span.parent_id == iterations[0].id
    assert tool_span.attrs["tool"] == "lookup_artist"

    run_record = sink.runs[-1]
    assert run_record.status == "completed"
    assert run_record.cost_usd == Decimal("0.001620")
    assert run_record.id == result.run_id


async def test_parallel_tool_calls_all_execute_in_one_iteration() -> None:
    provider = MockProvider(
        [
            MockTurn(
                tool_calls=[
                    _call("c1", stage_name="Nova Reyes"),
                    _call("c2", stage_name="Kaiya Marsh"),
                ]
            ),
            MockTurn(text="Both found."),
        ]
    )
    runtime, sink = _runtime(provider)
    result = await runtime.run(_agent(), "Look up both artists.")

    assert result.status == "completed"
    second = provider.calls[1]
    tool_messages = [m for m in second.messages if m.role == "tool"]
    assert [m.tool_call_id for m in tool_messages] == ["c1", "c2"]
    assert "Kaiya Marsh" in tool_messages[1].content
    assert [s.kind for s in sink.spans].count("tool_call") == 2


async def test_budget_exhaustion_ends_run_with_exhausted_status() -> None:
    provider = MockProvider(
        [
            MockTurn(tool_calls=[_call("c1", stage_name="A")]),
            MockTurn(tool_calls=[_call("c2", stage_name="B")]),
        ]
    )
    runtime, sink = _runtime(provider)
    # Each turn costs 0.000810 → the third iteration's budget check trips at 0.001620.
    agent = _agent(limits=RunLimits(max_iterations=6, run_budget_usd=Decimal("0.0015")))

    result = await runtime.run(agent, "Spend money.")

    assert result.status == "exhausted"
    assert result.final is None
    assert result.iterations == 2
    assert len(provider.calls) == 2  # no LLM call after the budget tripped
    guardrail = next(s for s in sink.spans if s.kind == "guardrail")
    assert guardrail.attrs["kind"] == "budget_exhausted"
    assert guardrail.parent_id is None  # recorded at run level
    assert sink.runs[-1].status == "exhausted"
    assert sink.runs[-1].cost_usd == Decimal("0.001620")


async def test_iteration_cap_ends_run_with_exhausted_status() -> None:
    provider = MockProvider(
        [
            MockTurn(tool_calls=[_call("c1", stage_name="A")]),
            MockTurn(tool_calls=[_call("c2", stage_name="B")]),
        ]
    )
    runtime, sink = _runtime(provider)
    agent = _agent(limits=RunLimits(max_iterations=2, run_budget_usd=Decimal("1")))

    result = await runtime.run(agent, "Loop forever.")

    assert result.status == "exhausted"
    assert result.iterations == 2
    assert len(provider.calls) == 2
    guardrail = next(s for s in sink.spans if s.kind == "guardrail")
    assert guardrail.attrs["kind"] == "iteration_cap"


async def test_invalid_tool_args_become_error_result_and_incident() -> None:
    provider = MockProvider(
        [
            MockTurn(tool_calls=[ToolCall(id="bad", name="lookup_artist", arguments={})]),
            MockTurn(text="Sorry, I mistyped.", match="rejected by guardrails"),
        ]
    )
    runtime, sink = _runtime(provider)
    result = await runtime.run(_agent(), "Look up someone.")

    assert result.status == "completed"  # the model saw the error and recovered
    error_result = provider.calls[1].messages[-1]
    assert error_result.role == "tool"
    assert error_result.is_error is True
    assert "stage_name" in error_result.content
    guardrail = next(s for s in sink.spans if s.kind == "guardrail")
    assert guardrail.attrs["kind"] == "invalid_tool_args"
    assert guardrail.attrs["tool"] == "lookup_artist"
    tool_span = next(s for s in sink.spans if s.kind == "tool_call")
    assert tool_span.attrs["status"] == "denied"


async def test_unknown_tool_is_denied_not_crashed() -> None:
    provider = MockProvider(
        [
            MockTurn(tool_calls=[ToolCall(id="x", name="drop_tables", arguments={})]),
            MockTurn(text="Understood."),
        ]
    )
    runtime, sink = _runtime(provider)
    result = await runtime.run(_agent(), "Try something sneaky.")

    assert result.status == "completed"
    guardrail = next(s for s in sink.spans if s.kind == "guardrail")
    assert guardrail.attrs["kind"] == "unknown_tool"
    denied = provider.calls[1].messages[-1]
    assert denied.is_error is True


async def test_tool_timeout_returns_error_result() -> None:
    class SlowParams(BaseModel):
        pass

    async def slow(_: SlowParams) -> str:
        await asyncio.sleep(0.2)
        return "never"

    slow_tool: Tool[SlowParams] = Tool(
        name="slow_tool", description="sleeps", params=SlowParams, handler=slow
    )
    provider = MockProvider(
        [
            MockTurn(tool_calls=[ToolCall(id="s1", name="slow_tool", arguments={})]),
            MockTurn(text="It timed out.", match="timed out"),
        ]
    )
    runtime, sink = _runtime(provider)
    agent = _agent(
        tools=[slow_tool],
        limits=RunLimits(max_iterations=4, run_budget_usd=Decimal("1"), tool_timeout_s=0.01),
    )

    result = await runtime.run(agent, "Run the slow tool.")

    assert result.status == "completed"
    tool_span = next(s for s in sink.spans if s.kind == "tool_call")
    assert tool_span.attrs["status"] == "timeout"
    timed_out = provider.calls[1].messages[-1]
    assert timed_out.is_error is True


async def test_tool_exception_returns_error_result() -> None:
    class BoomParams(BaseModel):
        pass

    async def boom(_: BoomParams) -> str:
        raise ValueError("kaboom")

    boom_tool: Tool[BoomParams] = Tool(
        name="boom", description="explodes", params=BoomParams, handler=boom
    )
    provider = MockProvider(
        [
            MockTurn(tool_calls=[ToolCall(id="b1", name="boom", arguments={})]),
            MockTurn(text="Tool failed.", match="kaboom"),
        ]
    )
    runtime, sink = _runtime(provider)
    result = await runtime.run(_agent(tools=[boom_tool]), "Run the boom tool.")

    assert result.status == "completed"
    tool_span = next(s for s in sink.spans if s.kind == "tool_call")
    assert tool_span.attrs["status"] == "error"
    assert "kaboom" in tool_span.attrs["error"]


async def test_duplicate_tool_results_are_deduped() -> None:
    provider = MockProvider(
        [
            MockTurn(tool_calls=[_call("c1", stage_name="Nova Reyes")]),
            MockTurn(tool_calls=[_call("c2", stage_name="Nova Reyes")]),
            MockTurn(text="Same artist twice."),
        ]
    )
    runtime, sink = _runtime(provider)
    result = await runtime.run(_agent(), "Look them up twice.")

    assert result.status == "completed"
    third = provider.calls[2]
    tool_messages = [m for m in third.messages if m.role == "tool"]
    assert "artist Nova Reyes has id 42" in tool_messages[0].content
    assert "duplicate of lookup_artist result #1" in tool_messages[1].content
    dedup_span = [s for s in sink.spans if s.kind == "tool_call"][1]
    assert dedup_span.attrs["deduped"] is True


async def test_oversize_result_is_compressed_by_utility_model() -> None:
    class DumpParams(BaseModel):
        pass

    async def dump(_: DumpParams) -> str:
        return "row " * 500  # ~2000 chars ≈ 500 tokens

    dump_tool: Tool[DumpParams] = Tool(
        name="dump", description="dumps rows", params=DumpParams, handler=dump
    )
    # Planner and utility share the mock provider: the script interleaves in call
    # order — planner tool turn, then the compression call, then the final turn.
    provider = MockProvider(
        [
            MockTurn(tool_calls=[ToolCall(id="d1", name="dump", arguments={})]),
            MockTurn(
                text="SUMMARY: 500 identical rows.",
                usage=Usage(input_tokens=600, output_tokens=20),
                match="Summarize",
            ),
            MockTurn(text="The dump was 500 rows.", match="SUMMARY: 500 identical rows."),
        ]
    )
    runtime, sink = _runtime(provider)
    agent = _agent(
        tools=[dump_tool],
        utility_model="mock-haiku",
        limits=RunLimits(max_iterations=4, run_budget_usd=Decimal("1"), max_result_tokens=100),
    )

    result = await runtime.run(agent, "Dump everything.")

    assert result.status == "completed"
    compression = next(s for s in sink.spans if s.kind == "compression")
    assert compression.attrs["method"] == "utility_model"
    assert compression.attrs["gen_ai.request.model"] == "mock-haiku"
    # Utility usage priced at mock-haiku rates: 600 x $1/M + 20 x $5/M = 0.000700.
    assert compression.attrs["cost_usd"] == Decimal("0.000700")
    tool_result = next(m for m in provider.calls[2].messages if m.role == "tool")
    assert "compressed" in tool_result.content
    assert "SUMMARY: 500 identical rows." in tool_result.content
    # Costs include planner turns AND the compression call — no silent LLM calls.
    assert result.cost_usd == Decimal("0.000810") * 2 + Decimal("0.000700")


async def test_oversize_result_truncates_without_utility_model() -> None:
    class DumpParams(BaseModel):
        pass

    async def dump(_: DumpParams) -> str:
        return "x" * 2000

    dump_tool: Tool[DumpParams] = Tool(
        name="dump", description="dumps", params=DumpParams, handler=dump
    )
    provider = MockProvider(
        [
            MockTurn(tool_calls=[ToolCall(id="d1", name="dump", arguments={})]),
            MockTurn(text="Truncated, noted.", match="truncated"),
        ]
    )
    runtime, sink = _runtime(provider)
    agent = _agent(
        tools=[dump_tool],
        limits=RunLimits(max_iterations=4, run_budget_usd=Decimal("1"), max_result_tokens=100),
    )

    result = await runtime.run(agent, "Dump everything.")

    assert result.status == "completed"
    compression = next(s for s in sink.spans if s.kind == "compression")
    assert compression.attrs["method"] == "truncate"
    tool_result = next(m for m in provider.calls[1].messages if m.role == "tool")
    assert len(tool_result.content) < 1000


async def test_truncated_tool_call_is_never_executed() -> None:
    """A max_tokens-cut reply's tool calls are discarded, not run (D-021).

    Streaming assembles tool arguments from partial JSON, so a cut mid-call can
    yield arguments that *validate* while being a prefix of what the model meant
    — executing them is acting on a call the model never finished. The arguments
    here are deliberately complete/valid: the discard must key off stop_reason,
    not off validation failing.
    """
    executed: list[str] = []

    class RecordingParams(BaseModel):
        stage_name: str

    async def recording(params: RecordingParams) -> str:
        executed.append(params.stage_name)
        return f"artist {params.stage_name} has id 42"

    tool: Tool[RecordingParams] = Tool(
        name="lookup_artist", description="find", params=RecordingParams, handler=recording
    )
    provider = MockProvider(
        [
            MockTurn(
                text="Submitting now.",
                tool_calls=[_call("t1", stage_name="Nova Reyes")],
                stop_reason="max_tokens",
            ),
            # The model sees the not-executed error result and re-issues the call.
            MockTurn(
                tool_calls=[_call("t2", stage_name="Nova Reyes")],
                match="not executed",
            ),
            MockTurn(text="Done.", match="artist Nova Reyes has id 42"),
        ]
    )
    runtime, sink = _runtime(provider)
    result = await runtime.run(_agent(tools=[tool]), "Look up Nova Reyes.")

    assert result.status == "completed"
    assert executed == ["Nova Reyes"]  # the truncated call never reached the handler
    # The wire history stays coherent: assistant turn with the cut call, then an
    # error tool_result for it.
    second = provider.calls[1]
    assert [m.role for m in second.messages] == ["user", "assistant", "tool"]
    error_result = second.messages[-1]
    assert error_result.tool_call_id == "t1"
    assert error_result.is_error is True
    assert "output-token limit" in error_result.content
    guardrail = next(s for s in sink.spans if s.kind == "guardrail")
    assert guardrail.attrs["kind"] == "output_truncated"
    assert "lookup_artist" in guardrail.attrs["detail"]
    # No tool_call span exists for the discarded call — only the real execution.
    tool_spans = [s for s in sink.spans if s.kind == "tool_call"]
    assert len(tool_spans) == 1


async def test_truncated_text_is_not_finalized_as_answer() -> None:
    """A max_tokens-cut text reply never becomes the final answer (D-021):
    run ddb797dc's multi_step-003 'completed' on a cut-off reply with no answer
    line. The runtime nudges the model to continue instead."""
    provider = MockProvider(
        [
            MockTurn(text="The reconciliation shows that", stop_reason="max_tokens"),
            MockTurn(text="ANSWER: done", match="not accepted as a final answer"),
        ]
    )
    runtime, sink = _runtime(provider)
    result = await runtime.run(_agent(), "Reconcile the period.")

    assert result.status == "completed"
    assert result.final is not None
    assert result.final.answer == "ANSWER: done"
    # The partial text stays in history (assistant), followed by the user nudge.
    second = provider.calls[1]
    assert [m.role for m in second.messages] == ["user", "assistant", "user"]
    assert second.messages[1].content == "The reconciliation shows that"
    guardrail = next(s for s in sink.spans if s.kind == "guardrail")
    assert guardrail.attrs["kind"] == "output_truncated"


async def test_truncation_on_last_iteration_exhausts_honestly() -> None:
    provider = MockProvider(
        [MockTurn(text="partial", stop_reason="max_tokens")],
    )
    runtime, sink = _runtime(provider)
    agent = _agent(limits=RunLimits(max_iterations=1, run_budget_usd=Decimal("1")))
    result = await runtime.run(agent, "Answer something long.")

    assert result.status == "exhausted"
    assert result.final is None
    kinds = [s.attrs["kind"] for s in sink.spans if s.kind == "guardrail"]
    assert kinds == ["output_truncated", "iteration_cap"]


async def test_provider_failure_ends_run_as_error() -> None:
    provider = MockProvider([MockTurn(tool_calls=[_call("c1", stage_name="A")])])
    runtime, sink = _runtime(provider)  # second call exhausts the script → ProviderError

    result = await runtime.run(_agent(), "Trigger a provider failure.")

    assert result.status == "error"
    assert result.final is None
    assert sink.runs[-1].status == "error"
    assert "exhausted" in str(sink.runs[-1].meta["error"])
    failed_llm = [s for s in sink.spans if s.kind == "llm_call"][-1]
    assert failed_llm.attrs["status"] == "error"


async def test_session_memory_context_precedes_the_user_message() -> None:
    session = SessionMemory(window=10)
    session.add(Message(role="user", content="Earlier question about Kaiya."))
    session.add(Message(role="assistant", content="Earlier answer."))

    provider = MockProvider([MockTurn(text="Continuing.", match="Earlier question about Kaiya.")])
    runtime, _ = _runtime(provider)
    result = await runtime.run(_agent(), "Follow-up question.", session=session)

    assert result.status == "completed"
    first = provider.calls[0]
    assert [m.content for m in first.messages] == [
        "Earlier question about Kaiya.",
        "Earlier answer.",
        "Follow-up question.",
    ]


async def test_custom_finalizer_types_the_answer() -> None:
    def finalize(text: str) -> FinalAnswer:
        if text.startswith("ABSTAIN"):
            return FinalAnswer(answer="", abstained=True)
        return FinalAnswer(answer=text)

    provider = MockProvider([MockTurn(text="ABSTAIN: no such artist.")])
    runtime, _ = _runtime(provider)
    result = await runtime.run(_agent(finalize=finalize), "Who is Zzyzx?")

    assert result.status == "completed"
    assert result.final is not None
    assert result.final.abstained is True


async def test_result_check_flags_annotates_and_never_blocks() -> None:
    """Phase 4 §4.6: a ResultCheck hit is a guardrail span + an annotated result —
    the model still sees the tool output and the run completes."""
    from backline.core.guardrails import Incident

    def canary_check(tool_name: str, result_text: str) -> Incident | None:
        if "id 42" in result_text:
            return Incident(kind="injection_suspected", detail="canary", tool=tool_name)
        return None

    provider = MockProvider(
        [
            MockTurn(tool_calls=[_call(stage_name="Nova Reyes")]),
            MockTurn(text="Done.", match="[guardrail notice — injection_suspected: canary]"),
        ]
    )
    runtime, sink = _runtime(provider)

    result = await runtime.run(_agent(result_checks=(canary_check,)), "Who is Nova Reyes?")

    assert result.status == "completed"
    guardrail_spans = [s for s in sink.spans if s.kind == "guardrail"]
    assert [s.attrs["kind"] for s in guardrail_spans] == ["injection_suspected"]
    tool_span = next(s for s in sink.spans if s.kind == "tool_call")
    assert tool_span.attrs["guardrail"] == "injection_suspected"
    # The annotated result reached the model (the second turn's match proved it),
    # prefixed but otherwise intact.
    tool_msg = provider.calls[1].messages[-1]
    assert tool_msg.role == "tool"
    assert tool_msg.content.startswith("[guardrail notice — injection_suspected")
    assert "artist Nova Reyes has id 42" in tool_msg.content
    assert tool_msg.is_error is False


async def test_trace_attrs_ride_into_run_meta() -> None:
    provider = MockProvider([MockTurn(text="hi")])
    runtime, sink = _runtime(provider)
    await runtime.run(_agent(trace_attrs={"prompt_sha256": "abc123def456"}), "hello")
    assert sink.runs[0].meta["prompt_sha256"] == "abc123def456"
    assert sink.runs[0].meta["model"] == "mock-sonnet"


async def test_pre_assigned_run_id_is_used() -> None:
    """Phase 6: the API announces the run id over SSE before the run starts."""
    import uuid

    provider = MockProvider([MockTurn(text="done")])
    sink = InMemorySink()
    runtime = AgentRuntime(
        providers={"mock": provider},
        registry=ModelRegistry.load(),
        tracer=Tracer([sink]),
    )
    assigned = uuid.uuid4()
    result = await runtime.run(_agent(), "go", run_id=assigned)
    assert result.run_id == assigned
    assert [r.id for r in sink.runs] == [assigned]
