from decimal import Decimal
from pathlib import Path

import pytest

from backline.providers.registry import DEFAULT_MODELS_YAML, ModelRegistry


def test_committed_registry_loads_with_exact_decimal_prices() -> None:
    registry = ModelRegistry.load()
    sonnet = registry.get("claude-sonnet-5")
    assert sonnet.provider == "anthropic"
    assert sonnet.context_window == 1_000_000
    assert sonnet.usd_per_mtok_in == Decimal("3.00")
    assert sonnet.usd_per_mtok_out == Decimal("15.00")
    assert isinstance(sonnet.usd_per_mtok_in, Decimal)


def test_registry_covers_the_three_anthropic_tiers_plus_local_and_mock() -> None:
    registry = ModelRegistry.load()
    assert {"claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5"} <= set(registry.ids)
    assert registry.get("local-qwen").provider == "openai_compat"
    assert registry.get("mock-sonnet").provider == "mock"
    assert registry.get("mock-haiku").provider == "mock"
    # Mock models are priced like their real-tier counterparts so budget tests are real.
    assert registry.get("mock-sonnet").usd_per_mtok_in == Decimal("3.00")


def test_unknown_model_raises_with_known_ids_listed() -> None:
    registry = ModelRegistry.load()
    with pytest.raises(KeyError, match=r"unknown model 'nope'.*claude-sonnet-5"):
        registry.get("nope")
    assert "nope" not in registry
    assert "claude-opus-5" in registry


def test_float_prices_are_rejected(tmp_path: Path) -> None:
    bad = tmp_path / "models.yaml"
    bad.write_text(
        "models:\n"
        "  m:\n"
        "    provider: mock\n"
        "    context_window: 1000\n"
        "    usd_per_mtok_in: 3.00\n"  # unquoted → YAML float → must be rejected
        '    usd_per_mtok_out: "15.00"\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="money is never float"):
        ModelRegistry.load(bad)


def test_default_path_points_at_committed_file() -> None:
    assert DEFAULT_MODELS_YAML.exists()
    assert DEFAULT_MODELS_YAML.name == "models.yaml"
