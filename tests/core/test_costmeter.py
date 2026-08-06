from decimal import Decimal

import pytest

from backline.core.costmeter import CostMeter
from backline.providers.base import Usage
from backline.providers.registry import ModelInfo, ModelRegistry


def _registry() -> ModelRegistry:
    return ModelRegistry.load()


def test_cost_is_exact_decimal_from_registry_prices() -> None:
    meter = CostMeter(_registry())
    cost = meter.add("mock-sonnet", Usage(input_tokens=123_456, output_tokens=7_890))
    # 123456 * 3.00/1M + 7890 * 15.00/1M — exact decimal arithmetic, no float drift.
    assert cost == Decimal("0.488718")
    assert isinstance(cost, Decimal)
    assert meter.total_usd == Decimal("0.488718")


def test_costs_accumulate_across_calls_and_models() -> None:
    meter = CostMeter(_registry())
    meter.add("mock-sonnet", Usage(input_tokens=1_000, output_tokens=1_000))  # 0.018
    meter.add("mock-haiku", Usage(input_tokens=1_000, output_tokens=1_000))  # 0.006
    assert meter.total_usd == Decimal("0.024")
    assert len(meter.calls) == 2
    assert meter.calls[0].model == "mock-sonnet"
    assert meter.calls[1].cost_usd == Decimal("0.006")


def test_sub_microdollar_costs_quantize_half_even() -> None:
    registry = ModelRegistry(
        {
            "tiny": ModelInfo(
                id="tiny",
                provider="mock",
                context_window=1000,
                usd_per_mtok_in=Decimal("0.10"),
                usd_per_mtok_out=Decimal("0"),
            )
        }
    )
    meter = CostMeter(registry)
    # 3 tokens * 0.10/1M = 0.0000003 → 6dp half-even → 0.000000 (policy: money6).
    assert meter.add("tiny", Usage(input_tokens=3, output_tokens=0)) == Decimal("0.000000")
    # 5 tokens → 0.0000005 → rounds half-even to the even digit → 0.000000
    assert meter.add("tiny", Usage(input_tokens=5, output_tokens=0)) == Decimal("0.000000")
    # 15 tokens → 0.0000015 → half-even → 0.000002
    assert meter.add("tiny", Usage(input_tokens=15, output_tokens=0)) == Decimal("0.000002")


def test_unknown_model_raises() -> None:
    meter = CostMeter(_registry())
    with pytest.raises(KeyError, match="unknown model"):
        meter.add("never-heard-of-it", Usage(input_tokens=1, output_tokens=1))
