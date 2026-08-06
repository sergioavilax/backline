"""Central runtime configuration, loaded from the environment (.env in dev)."""

from decimal import Decimal
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = "postgresql://backline:backline@localhost:5432/backline"
    world_seed: int = 20260805
    data_dir: str = "data"  # contracts, inbox drops, traces; /data inside compose

    anthropic_api_key: str = ""
    openai_compat_base_url: str = ""
    openai_compat_api_key: str = ""  # only needed for hosted OpenAI-format endpoints

    rerank: str = "on"

    # Budgets are money → Decimal, never float (invariant 1). Env values like "0.50"
    # parse exactly.
    run_budget_usd: Decimal = Decimal("0.50")
    eval_budget_usd: Decimal = Decimal("5.00")

    # AgentRuntime hard limits (BUILD_PLAN §4.2); per-agent overrides via RunLimits.
    max_iterations: int = 12
    tool_timeout_s: float = 30.0
    max_result_tokens: int = 2000


@lru_cache
def get_settings() -> Settings:
    return Settings()
