import pytest

from datagen.assemble import BuiltWorld, build_world
from datagen.config import WorldConfig, load_world_config

GOLDEN_SEED = 20260805  # the default WORLD_SEED; the committed golden file pins it


@pytest.fixture(scope="session")
def world_config() -> WorldConfig:
    return load_world_config()


@pytest.fixture(scope="session")
def built(world_config: WorldConfig) -> BuiltWorld:
    """One full in-memory world per test session (~12s) — no DB, no disk."""
    return build_world(world_config, GOLDEN_SEED)
