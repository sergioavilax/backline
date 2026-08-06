"""Central runtime configuration, loaded from the environment (.env in dev)."""

from decimal import Decimal
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


def repo_root() -> Path:
    """The repository root, located from this file — never from the process CWD.

    Anchors relative data/artifact paths (D-022): eval run ddb797dc's artifacts
    nested inside an older run's directory because ``data/evals`` resolved against
    wherever the CLI happened to be launched. Walks up to the ``pyproject.toml``
    marker; the fallback is the package's parent, which is the root in this repo's
    layout (deployments that relocate the package must configure absolute paths).
    """
    here = Path(__file__).resolve()
    for candidate in here.parents:
        if (candidate / "pyproject.toml").is_file():
            return candidate
    return here.parents[1]


def anchor_path(path: str | Path) -> Path:
    """Absolute paths pass through untouched; relative ones anchor at the repo root."""
    p = Path(path)
    return p if p.is_absolute() else repo_root() / p


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = "postgresql://backline:backline@localhost:5432/backline"
    world_seed: int = 20260805
    data_dir: str = "data"  # contracts, inbox drops, traces; /data inside compose

    anthropic_api_key: str = ""
    openai_compat_base_url: str = ""
    openai_compat_api_key: str = ""  # only needed for hosted OpenAI-format endpoints

    rerank: str = "on"

    # Model policy (§2, Phase 4): routing exists at two levels — agent selection
    # (the router) and model selection (planner/utility tiers per agent). The
    # planner drives agent loops (Sonnet-class); the utility model summarizes and
    # compresses (Haiku-class); the router classifies with the cheap tier.
    planner_model: str = "claude-sonnet-5"
    utility_model: str = "claude-haiku-4-5"
    router_model: str = "claude-haiku-4-5"
    # T3 LLM-as-judge model (§5.1): Sonnet-class; judge model + rubric hash are
    # recorded with every judged eval result.
    judge_model: str = "claude-sonnet-5"
    # Below this route confidence the router asks a clarifying question instead of
    # guessing an agent (§2).
    router_confidence_threshold: float = 0.6

    # Retrieval stack (§4.4). "hash" selects the deterministic offline embedder
    # (feature-hashed bag-of-words, 384-dim) used by tests and model-less environments;
    # "lexical" is the analogous offline reranker. Both real models run CPU-side via
    # the optional `embed` extra (sentence-transformers).
    embed_model: str = "BAAI/bge-small-en-v1.5"
    rerank_model: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"

    # sql_query tool policy knobs (§4.3): auto/max LIMIT and the EXPLAIN cost ceiling
    # (planner cost units — a full statement_lines aggregate is ~15K; a pathological
    # self-join is millions).
    sql_row_limit: int = 200
    sql_cost_ceiling: float = 150_000.0

    # Budgets are money → Decimal, never float (invariant 1). Env values like "0.50"
    # parse exactly.
    run_budget_usd: Decimal = Decimal("0.50")
    eval_budget_usd: Decimal = Decimal("5.00")

    # AgentRuntime hard limits (BUILD_PLAN §4.2); per-agent overrides via RunLimits.
    max_iterations: int = 12
    tool_timeout_s: float = 30.0
    max_result_tokens: int = 2000

    @property
    def data_path(self) -> Path:
        """``data_dir`` as an absolute path — relative values anchor at the repo
        root (D-022), absolute ones (compose's ``/data``) are authoritative as-is."""
        return anchor_path(self.data_dir)


@lru_cache
def get_settings() -> Settings:
    return Settings()
