"""Guardrail frame (BUILD_PLAN §4.6).

Phase 2 shipped the skeleton: hard run limits (iteration/budget caps), Pydantic
tool-arg validation, and the ``ToolCheck`` registration point Phase 3's SQL allowlist
plugs into. Phase 4 adds ``ResultCheck`` — policies over tool *results* (document-
injection flagging: retrieved contract text may contain instruction-shaped content;
the check raises an incident without blocking the result). Every denial or flag is an
``Incident`` the runtime records as a ``guardrail`` span, so incidents are visible in
the Trace Inspector, not buried in logs.
"""

from collections.abc import Callable, Sequence
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError

from backline.config import get_settings


class RunLimits(BaseModel):
    """Hard limits for one agent run (§4.2). Budget is money → Decimal."""

    model_config = ConfigDict(frozen=True)

    max_iterations: int = 12
    run_budget_usd: Decimal = Decimal("0.50")
    tool_timeout_s: float = 30.0
    max_result_tokens: int = 2000

    @classmethod
    def from_settings(cls) -> "RunLimits":
        s = get_settings()
        return cls(
            max_iterations=s.max_iterations,
            run_budget_usd=s.run_budget_usd,
            tool_timeout_s=s.tool_timeout_s,
            max_result_tokens=s.max_result_tokens,
        )


class Incident(BaseModel):
    """One guardrail denial; becomes a `guardrail` span in the trace."""

    model_config = ConfigDict(frozen=True)

    kind: str  # iteration_cap | budget_exhausted | unknown_tool | invalid_tool_args | custom
    detail: str
    tool: str | None = None


ToolCheck = Callable[[str, dict[str, Any]], Incident | None]
"""Pre-execution policy over (tool_name, raw_args) — a hit denies the call."""

ResultCheck = Callable[[str, str], Incident | None]
"""Post-execution policy over (tool_name, result_text) — a hit flags, never blocks:
the result still reaches the model (annotated by the runtime), the incident becomes a
``guardrail`` span. Injection detection lives here (§4.6)."""


class Guardrails:
    def __init__(
        self,
        limits: RunLimits,
        checks: Sequence[ToolCheck] = (),
        result_checks: Sequence[ResultCheck] = (),
    ) -> None:
        self.limits = limits
        self._checks = list(checks)
        self._result_checks = list(result_checks)

    def check_iteration(self, iteration: int) -> Incident | None:
        if iteration > self.limits.max_iterations:
            return Incident(
                kind="iteration_cap",
                detail=f"iteration {iteration} exceeds max_iterations={self.limits.max_iterations}",
            )
        return None

    def check_budget(self, spent_usd: Decimal) -> Incident | None:
        if spent_usd >= self.limits.run_budget_usd:
            return Incident(
                kind="budget_exhausted",
                detail=f"spent {spent_usd} USD of run_budget_usd={self.limits.run_budget_usd}",
            )
        return None

    def validate_tool_call(
        self,
        tool_name: str,
        params_model: type[BaseModel] | None,
        raw_args: dict[str, Any],
    ) -> tuple[BaseModel | None, Incident | None]:
        """Validate one tool call: known tool, well-typed args, registered policies."""
        if params_model is None:
            return None, Incident(
                kind="unknown_tool", detail=f"no tool named {tool_name!r}", tool=tool_name
            )
        try:
            validated = params_model.model_validate(raw_args)
        except ValidationError as exc:
            return None, Incident(
                kind="invalid_tool_args",
                detail=f"arguments failed validation: {exc.errors(include_url=False)!r}",
                tool=tool_name,
            )
        for check in self._checks:
            incident = check(tool_name, raw_args)
            if incident is not None:
                return None, incident
        return validated, None

    def check_tool_result(self, tool_name: str, result_text: str) -> Incident | None:
        """Run the post-execution policies; first hit wins (flag, don't block)."""
        for check in self._result_checks:
            incident = check(tool_name, result_text)
            if incident is not None:
                return incident
        return None
