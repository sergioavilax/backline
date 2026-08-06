"""Model registry: ``model_id → {provider, context_window, prices}`` (BUILD_PLAN §4.1).

Prices live in ``config/models.yaml`` — editable without code changes. The CostMeter
reads them from here; the Phase 7 benchmark derives $/query from usage x this table.
"""

from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, field_validator

DEFAULT_MODELS_YAML = Path(__file__).resolve().parents[2] / "config" / "models.yaml"

ProviderName = Literal["anthropic", "openai_compat", "mock"]


class ModelInfo(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    provider: ProviderName
    context_window: int
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


class ModelRegistry:
    def __init__(self, models: dict[str, ModelInfo]) -> None:
        self._models = models

    @classmethod
    def load(cls, path: Path | None = None) -> "ModelRegistry":
        source = path or DEFAULT_MODELS_YAML
        raw: dict[str, Any] = yaml.safe_load(source.read_text(encoding="utf-8"))
        models = {
            model_id: ModelInfo(id=model_id, **fields) for model_id, fields in raw["models"].items()
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
