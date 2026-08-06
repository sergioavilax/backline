"""Model registry: ``model_id → {provider, context_window, prices}`` (BUILD_PLAN §4.1).

Prices live in ``config/models.yaml`` — editable without code changes. The CostMeter
reads them from here; the Phase 7 benchmark derives $/query from usage x this table.

A model may price via a dated schedule (``pricing:`` tiers, each billing through its
inclusive UTC ``through`` date, one open-ended final tier) instead of flat fields.
``ModelRegistry.load`` resolves the tier for the date it loads (``on=`` pins it for
tests) and records the choice in ``ModelInfo.price_note``, so a scheduled transition —
e.g. claude-sonnet-5's intro pricing ending 2026-08-31 — happens on the calendar and
out loud, never by silently editing constants (D-017).
"""

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, field_validator

DEFAULT_MODELS_YAML = Path(__file__).resolve().parents[2] / "config" / "models.yaml"

ProviderName = Literal["anthropic", "openai_compat", "mock"]


class PriceTier(BaseModel):
    """One row of a dated price schedule; ``through`` is the last UTC day it bills."""

    model_config = ConfigDict(frozen=True)

    through: date | None = None  # None = the open-ended final tier
    usd_per_mtok_in: Decimal
    usd_per_mtok_out: Decimal

    @field_validator("usd_per_mtok_in", "usd_per_mtok_out", mode="before")
    @classmethod
    def _no_float_prices(cls, value: object) -> object:
        if isinstance(value, float):
            raise ValueError(
                "price parsed as float — quote it in models.yaml (money is never float)"
            )
        return value


class ModelInfo(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    provider: ProviderName
    context_window: int
    usd_per_mtok_in: Decimal
    usd_per_mtok_out: Decimal
    # For schedule-priced models: which tier applied and for which date it was
    # resolved ("" for flat-priced models). Surfaced in the eval runner banner.
    price_note: str = ""

    @field_validator("usd_per_mtok_in", "usd_per_mtok_out", mode="before")
    @classmethod
    def _no_float_prices(cls, value: object) -> object:
        if isinstance(value, float):
            raise ValueError(
                "price parsed as float — quote it in models.yaml (money is never float)"
            )
        return value


def _resolve_schedule(model_id: str, tiers: list[PriceTier], on: date) -> tuple[PriceTier, str]:
    """Pick the tier billing on ``on``; validate the schedule shape loudly."""
    if not tiers:
        raise ValueError(f"{model_id}: empty pricing schedule")
    for tier in tiers[:-1]:
        if tier.through is None:
            raise ValueError(
                f"{model_id}: only the final pricing tier may be open-ended (through: null)"
            )
    if tiers[-1].through is not None:
        raise ValueError(
            f"{model_id}: the final pricing tier must be open-ended — without it the "
            f"registry has no price after {tiers[-1].through}"
        )
    bounds = [tier.through for tier in tiers[:-1] if tier.through is not None]
    if bounds != sorted(set(bounds)):
        raise ValueError(f"{model_id}: pricing tiers must have strictly increasing `through` dates")
    for index, tier in enumerate(tiers):
        if tier.through is None or on <= tier.through:
            if tier.through is not None:
                note = f"dated pricing: tier through {tier.through} (resolved for {on})"
            else:
                since = bounds[-1] if bounds else None
                note = (
                    f"dated pricing: standard tier since {since} ended (resolved for {on})"
                    if since is not None and index > 0
                    else f"dated pricing (resolved for {on})"
                )
            return tier, note
    raise AssertionError("unreachable: final tier is open-ended")


def _model_info(model_id: str, fields: dict[str, Any], on: date) -> ModelInfo:
    if "pricing" not in fields:
        return ModelInfo(id=model_id, **fields)
    if "usd_per_mtok_in" in fields or "usd_per_mtok_out" in fields:
        raise ValueError(
            f"{model_id}: give flat usd_per_mtok fields or a dated `pricing` schedule, not both"
        )
    rest = {key: value for key, value in fields.items() if key != "pricing"}
    tiers = [PriceTier.model_validate(raw) for raw in fields["pricing"]]
    tier, note = _resolve_schedule(model_id, tiers, on)
    return ModelInfo(
        id=model_id,
        usd_per_mtok_in=tier.usd_per_mtok_in,
        usd_per_mtok_out=tier.usd_per_mtok_out,
        price_note=note,
        **rest,
    )


class ModelRegistry:
    def __init__(self, models: dict[str, ModelInfo]) -> None:
        self._models = models

    @classmethod
    def load(cls, path: Path | None = None, *, on: date | None = None) -> "ModelRegistry":
        """Load the registry with prices resolved for ``on`` (default: today, UTC)."""
        source = path or DEFAULT_MODELS_YAML
        resolved_on = on if on is not None else datetime.now(UTC).date()
        raw: dict[str, Any] = yaml.safe_load(source.read_text(encoding="utf-8"))
        models = {
            model_id: _model_info(model_id, fields, resolved_on)
            for model_id, fields in raw["models"].items()
        }
        return cls(models)

    def get(self, model_id: str) -> ModelInfo:
        try:
            return self._models[model_id]
        except KeyError:
            known = ", ".join(sorted(self._models))
            raise KeyError(f"unknown model {model_id!r} — registry has: {known}") from None

    def __contains__(self, model_id: str) -> bool:
        return model_id in self._models

    @property
    def ids(self) -> list[str]:
        return sorted(self._models)
