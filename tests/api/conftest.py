"""Fixtures for the Phase 6 API surface tests.

Everything runs keyless (invariant 8): the client fixture forces demo mode by
blanking the provider env vars, so chat drives MockProvider scripts through the real
router/runtime/tools against the seeded world (D-024). One TestClient session per
module keeps the lifespan (pool, tracer) alive across tests.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator

import asyncpg
import pytest
from fastapi.testclient import TestClient

import backline.config
from backline.rag.embed import run_embed
from backline.rag.embedder import HashingEmbedder
from tests.conftest import WorldEnv


@pytest.fixture(scope="session")
def chunked_world(world_env: WorldEnv) -> WorldEnv:
    """The seeded world plus the clause-chunk corpus (hash embedder — keyless)."""

    async def build() -> None:
        pool = await asyncpg.create_pool(world_env.database_url, min_size=1, max_size=2)
        assert pool is not None
        try:
            await run_embed(pool, data_dir=world_env.data_dir, embedder=HashingEmbedder())
        finally:
            await pool.close()

    asyncio.run(build())
    return world_env


@pytest.fixture
def api_client(chunked_world: WorldEnv, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("DATABASE_URL", chunked_world.database_url)
    monkeypatch.setenv("DATA_DIR", str(chunked_world.data_dir))
    # Force demo mode even on machines whose .env holds a real key: environment
    # values beat dotenv values, and empty means "not configured".
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setenv("OPENAI_COMPAT_BASE_URL", "")
    backline.config.get_settings.cache_clear()
    from backline.api.main import app

    try:
        with TestClient(app) as client:
            yield client
    finally:
        backline.config.get_settings.cache_clear()
