"""Raw-SQL migration runner.

Applies ``migrations/*.sql`` in lexicographic order, recording applied versions in a
``schema_migrations`` table. Each migration runs inside a single transaction. Chosen over
Alembic in D-000: this repo's schemas are hand-written SQL across four Postgres schemas,
and a small runner keeps the whole migration story inspectable in one file.

Usage: ``python -m backline.db.migrate`` (DATABASE_URL from the environment / .env).
"""

import asyncio
import sys
from pathlib import Path

import asyncpg

from backline.config import get_settings

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"

_ENSURE_TABLE = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version    TEXT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""


def discover_migrations(migrations_dir: Path = MIGRATIONS_DIR) -> list[Path]:
    """All migration files, sorted by filename (which encodes order)."""
    return sorted(migrations_dir.glob("*.sql"))


async def connect_with_retry(
    database_url: str, attempts: int = 10, delay_s: float = 1.0
) -> asyncpg.Connection:
    """Connect to Postgres, retrying briefly — the init container can race the db's first boot."""
    last_exc: Exception | None = None
    for _ in range(attempts):
        try:
            return await asyncpg.connect(database_url, timeout=5)
        except (OSError, asyncpg.PostgresError, TimeoutError) as exc:
            last_exc = exc
            await asyncio.sleep(delay_s)
    raise RuntimeError(f"could not connect to {database_url!r} after {attempts} attempts") from (
        last_exc
    )


async def run_migrations(
    database_url: str | None = None, migrations_dir: Path = MIGRATIONS_DIR
) -> list[str]:
    """Apply pending migrations; return the list of versions applied this run."""
    url = database_url or get_settings().database_url
    conn = await connect_with_retry(url)
    applied_now: list[str] = []
    try:
        await conn.execute(_ENSURE_TABLE)
        rows = await conn.fetch("SELECT version FROM schema_migrations")
        already_applied = {r["version"] for r in rows}
        for path in discover_migrations(migrations_dir):
            version = path.stem
            if version in already_applied:
                continue
            sql = path.read_text(encoding="utf-8")
            async with conn.transaction():
                await conn.execute(sql)
                await conn.execute("INSERT INTO schema_migrations (version) VALUES ($1)", version)
            applied_now.append(version)
    finally:
        await conn.close()
    return applied_now


def main() -> int:
    applied = asyncio.run(run_migrations())
    if applied:
        print(f"applied {len(applied)} migration(s): {', '.join(applied)}")
    else:
        print("migrations: up to date, nothing to apply")
    return 0


if __name__ == "__main__":
    sys.exit(main())
