import asyncio
import os
import subprocess
import sys
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import asyncpg
import pytest

requires_postgres = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — Postgres-backed tests run in CI (service container) "
    "or against `docker compose up` locally",
)

REPO_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class WorldEnv:
    """A fully seeded world: Postgres content + the on-disk corpus (contracts, inbox)."""

    database_url: str
    data_dir: Path


@pytest.fixture(scope="session")
def world_env(tmp_path_factory: pytest.TempPathFactory) -> WorldEnv:
    """Seed the world once for the Phase 3+ tool/RAG/eval integration tests.

    Deliberately independent of the datagen suite's own seed fixture (that one asserts
    the seeding *process*; this one just needs a seeded world): re-seeding is a truncate
    + reload of byte-identical content, so running both in one session is safe in any
    order. Only used under ``requires_postgres``.
    """
    url = os.environ["DATABASE_URL"]
    from backline.db.migrate import run_migrations

    asyncio.run(run_migrations(url))
    data_dir = tmp_path_factory.mktemp("phase3-world")
    result = subprocess.run(
        [sys.executable, "-m", "datagen", "seed"],
        cwd=REPO_ROOT,
        env={**os.environ, "DATA_DIR": str(data_dir)},
        capture_output=True,
        text=True,
        check=False,
        timeout=600,
    )
    assert result.returncode == 0, f"seed failed:\n{result.stdout}\n{result.stderr}"
    return WorldEnv(database_url=url, data_dir=data_dir)


@pytest.fixture
async def pool(world_env: WorldEnv) -> AsyncIterator[asyncpg.Pool]:
    """A per-test asyncpg pool against the seeded world (pools are loop-bound)."""
    pool = await asyncpg.create_pool(world_env.database_url, min_size=1, max_size=4)
    try:
        yield pool
    finally:
        await pool.close()
