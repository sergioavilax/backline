"""End-to-end seed against a real Postgres (skips without DATABASE_URL).

Runs the actual CLI (`python -m datagen seed`) into a temp DATA_DIR, then checks the
BUILD_PLAN Phase 1 DoD from the outside: row counts, the DB-derived fingerprint against
the committed golden, on-disk drops/PDFs against the committed file hashes, the anomaly
registry, and `emit-period`. Test order inside this module matters: emit-period mutates
state, so it runs last.
"""

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import asyncpg
import pytest

from backline.db.migrate import run_migrations
from datagen.config import load_world_config
from datagen.feeds import drop_filename
from datagen.fingerprint import fingerprint_files, fingerprint_from_db
from tests.conftest import requires_postgres

REPO_ROOT = Path(__file__).resolve().parents[2]
GOLDEN = json.loads(
    (REPO_ROOT / "tests" / "golden" / "world_fingerprint.json").read_text(encoding="utf-8")
)


def _run_datagen(*args: str, data_dir: Path) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "DATA_DIR": str(data_dir)}
    return subprocess.run(
        [sys.executable, "-m", "datagen", *args],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=600,
    )


async def _fetchval(query: str) -> object:
    conn = await asyncpg.connect(os.environ["DATABASE_URL"])
    try:
        return await conn.fetchval(query)
    finally:
        await conn.close()


def fetchval(query: str) -> object:
    return asyncio.run(_fetchval(query))


@pytest.fixture(scope="session")
def seeded_data_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    url = os.environ["DATABASE_URL"]
    asyncio.run(run_migrations(url))
    data_dir = tmp_path_factory.mktemp("worlddata")
    result = _run_datagen("seed", data_dir=data_dir)
    assert result.returncode == 0, f"seed failed:\n{result.stdout}\n{result.stderr}"
    return data_dir


@requires_postgres
def test_seed_loads_the_world(seeded_data_dir: Path) -> None:
    assert fetchval("SELECT count(*) FROM label.artists") == 150
    lines = fetchval("SELECT count(*) FROM label.statement_lines")
    assert isinstance(lines, int) and lines >= 450_000
    assert fetchval("SELECT count(*) FROM truth.expected_ledger") == 1800
    assert fetchval("SELECT count(*) FROM truth.anomaly_registry") == 40
    assert fetchval("SELECT count(*) FROM label.statements WHERE status = 'ingested'") == 72
    assert fetchval("SELECT count(*) FROM label.fx_rates") == 48


@requires_postgres
def test_db_fingerprint_matches_golden(seeded_data_dir: Path) -> None:
    tables = asyncio.run(fingerprint_from_db(os.environ["DATABASE_URL"]))
    assert tables == GOLDEN["tables"], (
        "Postgres content differs from the committed golden fingerprint — either the "
        "world drifted or the DB round-trip lost fidelity"
    )


@requires_postgres
def test_files_match_golden(seeded_data_dir: Path) -> None:
    assert fingerprint_files(seeded_data_dir) == GOLDEN["files"]


@requires_postgres
def test_registry_borderline_semantics(seeded_data_dir: Path) -> None:
    borderline = fetchval(
        "SELECT count(*) FROM truth.anomaly_registry WHERE expected_flag_kind IS NULL"
    )
    assert borderline == 2
    orphans = fetchval(
        """
        SELECT count(*) FROM truth.anomaly_registry r
        LEFT JOIN label.statement_lines sl ON sl.id = r.statement_line_id
        WHERE sl.id IS NULL
        """
    )
    assert orphans == 0


@requires_postgres
def test_ledger_sanity_in_db(seeded_data_dir: Path) -> None:
    assert fetchval("SELECT count(*) FROM truth.expected_ledger WHERE gross < 0") == 0
    assert fetchval("SELECT count(*) FROM truth.expected_ledger WHERE balance_after < 0") == 0
    assert fetchval("SELECT count(*) FROM truth.expected_ledger WHERE recouped > gross") == 0
    paid = fetchval(
        "SELECT count(DISTINCT artist_id) FROM truth.expected_ledger WHERE net_payable > 0"
    )
    assert isinstance(paid, int) and paid >= 100  # most artists see some payout in a year


@requires_postgres
def test_emit_period_drops_a_new_month(seeded_data_dir: Path) -> None:
    result = _run_datagen("emit-period", "2026-07", data_dir=seeded_data_dir)
    assert result.returncode == 0, result.stderr
    assert "status=received" in result.stdout
    assert fetchval("SELECT count(*) FROM label.statements WHERE period = '2026-07'") == 6
    drops = [
        seeded_data_dir / "inbox" / drop_filename(feed.dialect, "2026-07")
        for feed in load_world_config().feeds.values()
    ]
    assert len(drops) == 6
    assert all(p.is_file() for p in drops)
    # Deterministic: a second emit rewrites identical bytes and inserts nothing.
    before = sorted((p.name, p.read_bytes()) for p in drops)
    again = _run_datagen("emit-period", "2026-07", data_dir=seeded_data_dir)
    assert again.returncode == 0
    assert "0 statement rows recorded" in again.stdout
    after = sorted((p.name, p.read_bytes()) for p in drops)
    assert after == before


@requires_postgres
def test_emit_period_refuses_seeded_window(seeded_data_dir: Path) -> None:
    result = _run_datagen("emit-period", "2026-03", data_dir=seeded_data_dir)
    assert result.returncode == 1
    assert "inside the seeded window" in result.stderr
