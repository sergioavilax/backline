from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from backline.providers.registry import DEFAULT_MODELS_YAML, ModelRegistry


def test_committed_registry_loads_with_exact_decimal_prices() -> None:
    registry = ModelRegistry.load(on=date(2026, 9, 1))
    sonnet = registry.get("claude-sonnet-5")
    assert sonnet.provider == "anthropic"
    assert sonnet.context_window == 1_000_000
    assert sonnet.usd_per_mtok_in == Decimal("3.00")
    assert sonnet.usd_per_mtok_out == Decimal("15.00")
    assert isinstance(sonnet.usd_per_mtok_in, Decimal)


def test_sonnet_intro_pricing_applies_through_august_2026() -> None:
    """The Sept 1 transition is explicit: intro 2/10 bills through 2026-08-31 (UTC),
    the 3/15 sticker from 2026-09-01 — and the resolved tier says which it is."""
    intro = ModelRegistry.load(on=date(2026, 8, 31)).get("claude-sonnet-5")
    assert intro.usd_per_mtok_in == Decimal("2.00")
    assert intro.usd_per_mtok_out == Decimal("10.00")
    assert "2026-08-31" in intro.price_note

    standard = ModelRegistry.load(on=date(2026, 9, 1)).get("claude-sonnet-5")
    assert standard.usd_per_mtok_in == Decimal("3.00")
    assert standard.usd_per_mtok_out == Decimal("15.00")
    assert "standard" in standard.price_note

    # Flat-priced models carry no schedule note and never move on a calendar day.
    for on in (date(2026, 8, 31), date(2026, 9, 1)):
        mock = ModelRegistry.load(on=on).get("mock-sonnet")
        assert mock.usd_per_mtok_in == Decimal("3.00")
        assert mock.price_note == ""


def test_default_load_resolves_prices_for_today_utc() -> None:
    today = datetime.now(UTC).date()
    assert (
        ModelRegistry.load().get("claude-sonnet-5").usd_per_mtok_in
        == ModelRegistry.load(on=today).get("claude-sonnet-5").usd_per_mtok_in
    )


def test_malformed_price_schedules_are_rejected(tmp_path: Path) -> None:
    def yaml_for(pricing: str) -> Path:
        file = tmp_path / "models.yaml"
        file.write_text(
            "models:\n  m:\n    provider: mock\n    context_window: 1000\n    pricing:\n" + pricing,
            encoding="utf-8",
        )
        return file

    no_terminal = yaml_for(
        '      - through: "2026-08-31"\n'
        '        usd_per_mtok_in: "2.00"\n'
        '        usd_per_mtok_out: "10.00"\n'
    )
    with pytest.raises(ValueError, match="must be open-ended"):
        ModelRegistry.load(no_terminal)

    out_of_order = yaml_for(
        '      - through: "2026-09-30"\n'
        '        usd_per_mtok_in: "2.00"\n'
        '        usd_per_mtok_out: "10.00"\n'
        '      - through: "2026-08-31"\n'
        '        usd_per_mtok_in: "2.50"\n'
        '        usd_per_mtok_out: "12.00"\n'
        '      - usd_per_mtok_in: "3.00"\n'
        '        usd_per_mtok_out: "15.00"\n'
    )
    with pytest.raises(ValueError, match="strictly increasing"):
        ModelRegistry.load(out_of_order)

    both = tmp_path / "both.yaml"
    both.write_text(
        "models:\n  m:\n    provider: mock\n    context_window: 1000\n"
        '    usd_per_mtok_in: "3.00"\n'
        '    usd_per_mtok_out: "15.00"\n'
        "    pricing:\n"
        '      - usd_per_mtok_in: "2.00"\n'
        '        usd_per_mtok_out: "10.00"\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="not both"):
        ModelRegistry.load(both)

    float_tier = yaml_for('      - usd_per_mtok_in: 2.00\n        usd_per_mtok_out: "10.00"\n')
    with pytest.raises(ValueError, match="money is never float"):
        ModelRegistry.load(float_tier)


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
