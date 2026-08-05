"""Central runtime configuration, loaded from the environment (.env in dev)."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = "postgresql://backline:backline@localhost:5432/backline"
    world_seed: int = 20260805

    anthropic_api_key: str = ""
    openai_compat_base_url: str = ""

    rerank: str = "on"
    run_budget_usd: float = 0.50
    eval_budget_usd: float = 5.00


@lru_cache
def get_settings() -> Settings:
    return Settings()
