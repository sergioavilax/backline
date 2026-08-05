import os

import asyncpg

from backline.db.migrate import discover_migrations, run_migrations
from tests.conftest import requires_postgres


def test_baseline_migration_exists() -> None:
    versions = [p.stem for p in discover_migrations()]
    assert versions[0] == "0001_baseline"
    assert versions == sorted(versions)


@requires_postgres
async def test_migrations_apply_and_are_idempotent() -> None:
    url = os.environ["DATABASE_URL"]

    first = await run_migrations(url)
    second = await run_migrations(url)
    assert second == []  # everything recorded; nothing re-applied

    conn = await asyncpg.connect(url)
    try:
        rows = await conn.fetch("SELECT version FROM schema_migrations ORDER BY version")
    finally:
        await conn.close()
    recorded = [r["version"] for r in rows]
    assert "0001_baseline" in recorded
    # Every discovered migration is recorded, whether applied this run or earlier.
    assert {p.stem for p in discover_migrations()} <= set(recorded)
    assert set(first) <= set(recorded)


@requires_postgres
async def test_readyz_ok_against_live_db() -> None:
    import backline.config

    backline.config.get_settings.cache_clear()
    try:
        from fastapi.testclient import TestClient

        from backline.api.main import app

        with TestClient(app) as client:
            resp = client.get("/readyz")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok", "database": "ok"}
    finally:
        backline.config.get_settings.cache_clear()
