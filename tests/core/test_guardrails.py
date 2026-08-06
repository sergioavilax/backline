from decimal import Decimal

from pydantic import BaseModel

from backline.core.guardrails import Guardrails, Incident, RunLimits


class LookupParams(BaseModel):
    stage_name: str
    limit: int = 5


def _rails(**overrides: object) -> Guardrails:
    limits = RunLimits(
        max_iterations=3,
        run_budget_usd=Decimal("0.10"),
        tool_timeout_s=1.0,
        max_result_tokens=100,
    )
    return Guardrails(limits, **overrides)  # type: ignore[arg-type]


def test_iteration_cap() -> None:
    rails = _rails()
    assert rails.check_iteration(1) is None
    assert rails.check_iteration(3) is None
    incident = rails.check_iteration(4)
    assert incident is not None
    assert incident.kind == "iteration_cap"


def test_budget_cap_trips_at_or_above_budget() -> None:
    rails = _rails()
    assert rails.check_budget(Decimal("0.099999")) is None
    incident = rails.check_budget(Decimal("0.10"))
    assert incident is not None
    assert incident.kind == "budget_exhausted"
    assert "0.10" in incident.detail


def test_tool_arg_validation_happy_path() -> None:
    rails = _rails()
    validated, incident = rails.validate_tool_call(
        "lookup", LookupParams, {"stage_name": "Nova Reyes"}
    )
    assert incident is None
    assert isinstance(validated, LookupParams)
    assert validated.stage_name == "Nova Reyes"
    assert validated.limit == 5


def test_tool_arg_validation_rejects_bad_args_with_detail() -> None:
    rails = _rails()
    validated, incident = rails.validate_tool_call("lookup", LookupParams, {"limit": "not-an-int"})
    assert validated is None
    assert incident is not None
    assert incident.kind == "invalid_tool_args"
    assert incident.tool == "lookup"
    assert "stage_name" in incident.detail  # missing required field is named


def test_unknown_tool_is_an_incident() -> None:
    rails = _rails()
    validated, incident = rails.validate_tool_call("no_such_tool", None, {})
    assert validated is None
    assert incident is not None
    assert incident.kind == "unknown_tool"


def test_registered_checks_extend_the_frame() -> None:
    # The Phase 3/4 extension point: SQL policy, injection flags, etc. plug in here.
    def deny_truth_schema(tool: str, args: dict[str, object]) -> Incident | None:
        if tool == "sql_query" and "truth." in str(args.get("query", "")):
            return Incident(kind="sql_policy", detail="truth schema is off-limits", tool=tool)
        return None

    rails = _rails(checks=[deny_truth_schema])

    class SqlParams(BaseModel):
        query: str

    validated, incident = rails.validate_tool_call(
        "sql_query", SqlParams, {"query": "SELECT * FROM truth.expected_ledger"}
    )
    assert validated is None
    assert incident is not None
    assert incident.kind == "sql_policy"

    validated, incident = rails.validate_tool_call(
        "sql_query", SqlParams, {"query": "SELECT 1 FROM label.artists"}
    )
    assert incident is None
    assert validated is not None


def test_limits_default_from_settings() -> None:
    limits = RunLimits.from_settings()
    assert limits.max_iterations == 12
    assert limits.run_budget_usd == Decimal("0.50")
    assert isinstance(limits.run_budget_usd, Decimal)
