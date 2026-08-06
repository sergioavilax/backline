"""CostMeter — prices every LLM call from the model registry (BUILD_PLAN §4.1/§4.7).

Cost is money, so it is ``Decimal`` end-to-end (invariant 1) and quantizes through the
repo's one rounding policy (``money6``). The Phase 7 benchmark derives $/query from
exactly this arithmetic.
"""

from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from backline.providers.base import Usage
from backline.providers.registry import ModelRegistry
from backline.royaltycalc.rounding import money6

_MTOK = Decimal(1_000_000)


class CostedCall(BaseModel):
    model_config = ConfigDict(frozen=True)

    model: str
    usage: Usage
    cost_usd: Decimal


class CostMeter:
    def __init__(self, registry: ModelRegistry) -> None:
        self._registry = registry
        self.calls: list[CostedCall] = []
        self.total_usd = Decimal("0")

    def add(self, model_id: str, usage: Usage) -> Decimal:
        """Price one call, accumulate it, and return its cost."""
        info = self._registry.get(model_id)
        cost = money6(
            (
                Decimal(usage.input_tokens) * info.usd_per_mtok_in
                + Decimal(usage.output_tokens) * info.usd_per_mtok_out
            )
            / _MTOK
        )
        self.calls.append(CostedCall(model=model_id, usage=usage, cost_usd=cost))
        self.total_usd += cost
        return cost
