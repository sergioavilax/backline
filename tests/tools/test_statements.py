"""Reconciler tools: dialect normalizer, ingest_statement, match_lines, submit_batch.

The normalizer's bar is exactness: parsing a rendered drop must recover the canonical
line values byte-for-value across all six CSV dialects, pinned against what datagen
loaded into Postgres — including recomputing datagen's own line_hash. The ingest path
is exercised through the real flow: `datagen emit-period` drops a fresh month, the
tool stages it (staging only — invariant 5), match_lines partitions it, submit_batch
proposes. Skips without DATABASE_URL.
"""

import os
import subprocess
import sys
import uuid
from decimal import Decimal
from pathlib import Path

import asyncpg
import pytest

from backline.config import get_settings
from backline.core.runcontext import current_run_id
from backline.tools.context import ToolContext
from backline.tools.ledger import compute_ledger_slice
from backline.tools.normalizer import parse_drop
from backline.tools.statements import (
    build_ingest_statement_tool,
    build_match_lines_tool,
    build_submit_batch_tool,
)
from tests.conftest import REPO_ROOT, WorldEnv, requires_postgres

pytestmark = requires_postgres

DIALECTS = [
    "kinetic_us",
    "meridian_eu",
    "pulsewave_uk",
    "northstar_retail",
    "vantage_jp",
    "syncbridge_lic",
]
EMIT_PERIOD = "2026-07"


@pytest.fixture
def ctx(pool: asyncpg.Pool, world_env: WorldEnv) -> ToolContext:
    settings = get_settings().model_copy(update={"data_dir": str(world_env.data_dir)})
    return ToolContext(pool=pool, settings=settings)


@pytest.mark.parametrize("dialect", DIALECTS)
async def test_normalizer_roundtrips_every_dialect(
    dialect: str, pool: asyncpg.Pool, world_env: WorldEnv
) -> None:
    statement = await pool.fetchrow(
        """
        SELECT s.id, s.period, s.raw_path FROM label.statements s
        JOIN label.distributors d ON d.id = s.distributor_id
        WHERE d.dialect = $1 AND s.period = '2026-02'
        """,
        dialect,
    )
    text = (world_env.data_dir / "inbox" / Path(statement["raw_path"]).name).read_text(
        encoding="utf-8"
    )
    parsed, errors = parse_drop(dialect, text)
    assert errors == []

    db_rows = await pool.fetch(
        "SELECT period, isrc, upc, store, territory, units, gross_amount, currency, "
        "line_hash FROM label.statement_lines WHERE statement_id = $1 ORDER BY id",
        statement["id"],
    )
    assert len(parsed) == len(db_rows)

    def key(
        period: str,
        isrc: str,
        upc: object,
        store: str,
        territory: str,
        units: int,
        gross: Decimal,
        currency: str,
    ) -> tuple[object, ...]:
        return (period, isrc, upc, store, territory, units, gross, currency)

    parsed_keys = sorted(
        key(p.period, p.isrc, p.upc, p.store, p.territory, p.units, p.gross_amount, p.currency)
        for p in parsed
    )
    db_keys = sorted(
        key(
            r["period"],
            r["isrc"],
            r["upc"],
            r["store"],
            r["territory"],
            r["units"],
            r["gross_amount"],
            r["currency"],
        )
        for r in db_rows
    )
    assert parsed_keys == db_keys

    # Recomputed hashes match datagen's stored ones (injected anomaly lines carry '').
    stored_hashes = sorted(r["line_hash"] for r in db_rows if r["line_hash"])
    recomputed = sorted(p.line_hash for p in parsed)
    assert set(stored_hashes) <= set(recomputed)


def test_normalizer_reports_malformed_rows_without_dying() -> None:
    text = (
        "report_period,isrc,upc,store,country,quantity,net_revenue,currency\n"
        "2026-02,QZFBR2600001,036847000010,Streamora,US,100,12.345600,USD\n"
        "2026-02,QZFBR2600002,036847000027,Streamora,US,not_a_number,9.99,USD\n"
        "totally,broken\n"
    )
    parsed, errors = parse_drop("kinetic_us", text)
    assert len(parsed) == 1
    assert len(errors) == 2
    assert any("row 3" in e for e in errors)


@pytest.fixture(scope="module")
def emitted(world_env: WorldEnv) -> str:
    result = subprocess.run(
        [sys.executable, "-m", "datagen", "emit-period", EMIT_PERIOD],
        cwd=REPO_ROOT,
        env={**os.environ, "DATA_DIR": str(world_env.data_dir)},
        capture_output=True,
        text=True,
        check=False,
        timeout=600,
    )
    assert result.returncode == 0, result.stderr
    return EMIT_PERIOD


async def test_emit_period_recorded_fx_for_the_new_month(emitted: str, pool: asyncpg.Pool) -> None:
    n = await pool.fetchval("SELECT count(*) FROM label.fx_rates WHERE period = $1", emitted)
    assert n == 4  # USD, EUR, GBP, JPY from world.yaml


async def test_ingest_stages_a_fresh_drop(
    emitted: str, ctx: ToolContext, pool: asyncpg.Pool
) -> None:
    tool = build_ingest_statement_tool(ctx)
    out = await tool.handler(tool.params(path=f"data/inbox/kinetic_digital_{emitted}.csv"))
    assert "staged" in out.lower()
    assert "staging.ingested_lines" in out

    statement_id = await pool.fetchval(
        """
        SELECT s.id FROM label.statements s JOIN label.distributors d
        ON d.id = s.distributor_id WHERE d.dialect = 'kinetic_us' AND s.period = $1
        """,
        emitted,
    )
    staged = await pool.fetchval(
        "SELECT count(*) FROM staging.ingested_lines WHERE statement_id = $1", statement_id
    )
    assert isinstance(staged, int) and staged > 1000
    assert f"statement {statement_id}" in out

    # label.* untouched (invariant 5): the statement stays 'received', no lines leaked.
    status = await pool.fetchval("SELECT status FROM label.statements WHERE id = $1", statement_id)
    assert status == "received"
    leaked = await pool.fetchval(
        "SELECT count(*) FROM label.statement_lines WHERE statement_id = $1", statement_id
    )
    assert leaked == 0

    # Re-ingest replaces its own staging rows, never duplicates.
    await tool.handler(tool.params(path=f"data/inbox/kinetic_digital_{emitted}.csv"))
    staged_again = await pool.fetchval(
        "SELECT count(*) FROM staging.ingested_lines WHERE statement_id = $1", statement_id
    )
    assert staged_again == staged


async def test_ingest_refuses_already_ingested_statements(ctx: ToolContext) -> None:
    tool = build_ingest_statement_tool(ctx)
    out = await tool.handler(tool.params(path="data/inbox/kinetic_digital_2026-02.csv"))
    assert "already ingested" in out.lower()


async def test_ingest_unknown_drop_reports(ctx: ToolContext) -> None:
    tool = build_ingest_statement_tool(ctx)
    out = await tool.handler(tool.params(path="data/inbox/no_such_drop.csv"))
    assert "no statement" in out.lower()


async def test_match_lines_on_staged_statement_finds_the_injected_unknowns(
    emitted: str, ctx: ToolContext, pool: asyncpg.Pool
) -> None:
    ingest = build_ingest_statement_tool(ctx)
    await ingest.handler(ingest.params(path=f"data/inbox/kinetic_digital_{emitted}.csv"))
    statement_id = await pool.fetchval(
        """
        SELECT s.id FROM label.statements s JOIN label.distributors d
        ON d.id = s.distributor_id WHERE d.dialect = 'kinetic_us' AND s.period = $1
        """,
        emitted,
    )
    tool = build_match_lines_tool(ctx)
    out = await tool.handler(tool.params(statement_id=statement_id))
    assert "unmatched" in out.lower()
    # emit-period injects unknown-ISRC anomalies into the fresh month (D-005).
    assert "QZFBR" in out


async def test_match_lines_on_seeded_statement_with_unknown_isrc(
    ctx: ToolContext, pool: asyncpg.Pool
) -> None:
    case = await pool.fetchrow(
        """
        SELECT sl.statement_id, sl.isrc FROM truth.anomaly_registry r
        JOIN label.statement_lines sl ON sl.id = r.statement_line_id
        WHERE r.kind = 'unknown_isrc' LIMIT 1
        """
    )
    tool = build_match_lines_tool(ctx)
    out = await tool.handler(tool.params(statement_id=case["statement_id"]))
    assert case["isrc"] in out  # the alien ISRC is listed as unmatched


async def test_match_lines_requires_ingestion_first(
    emitted: str, ctx: ToolContext, pool: asyncpg.Pool
) -> None:
    statement_id = await pool.fetchval(
        """
        SELECT s.id FROM label.statements s JOIN label.distributors d
        ON d.id = s.distributor_id WHERE d.dialect = 'vantage_jp' AND s.period = $1
        """,
        emitted,
    )
    await pool.execute("DELETE FROM staging.ingested_lines WHERE statement_id = $1", statement_id)
    tool = build_match_lines_tool(ctx)
    out = await tool.handler(tool.params(statement_id=statement_id))
    assert "ingest_statement" in out


async def test_submit_batch_writes_staging_only_and_stamps_the_run(
    ctx: ToolContext, pool: asyncpg.Pool
) -> None:
    run_id = uuid.uuid4()
    await pool.execute(
        "INSERT INTO app.runs (id, agent, status, started_at) "
        "VALUES ($1, 'reconciler-test', 'running', now())",
        run_id,
    )
    tool = build_submit_batch_tool(ctx)
    token = current_run_id.set(run_id)
    try:
        out = await tool.handler(
            tool.params.model_validate(
                {
                    "period": "2026-06",
                    "allocations": [
                        {"artist_id": 1, "net_payable": "123.45", "line_detail": {"n_lines": 10}},
                        {"artist_id": 2, "net_payable": 67.8},
                    ],
                    "flags": [
                        {
                            "kind": "duplicate_line",
                            "severity": "warning",
                            "payload": {"line_ids": [1, 2]},
                        },
                        {"kind": "unknown_isrc", "severity": "error", "payload": {}},
                    ],
                }
            )
        )
    finally:
        current_run_id.reset(token)

    assert "proposed" in out.lower()
    assert "approve" in out.lower()  # tells the model a human approves, not the agent
    batch = await pool.fetchrow(
        "SELECT id, period, submitted_by_run, status, summary FROM "
        "staging.statement_batches ORDER BY id DESC LIMIT 1"
    )
    assert batch["status"] == "proposed"
    assert batch["submitted_by_run"] == run_id
    assert f"batch {batch['id']}" in out

    allocations = await pool.fetch(
        "SELECT artist_id, net_payable FROM staging.proposed_allocations "
        "WHERE batch_id = $1 ORDER BY artist_id",
        batch["id"],
    )
    assert [(a["artist_id"], a["net_payable"]) for a in allocations] == [
        (1, Decimal("123.450000")),
        (2, Decimal("67.800000")),
    ]
    flags = await pool.fetch(
        "SELECT kind, severity FROM staging.flags WHERE batch_id = $1 ORDER BY id",
        batch["id"],
    )
    assert [(f["kind"], f["severity"]) for f in flags] == [
        ("duplicate_line", "warning"),
        ("unknown_isrc", "error"),
    ]


async def test_submit_batch_rejects_duplicate_artists_and_bad_severity(
    ctx: ToolContext,
) -> None:
    tool = build_submit_batch_tool(ctx)
    with pytest.raises(ValueError, match="artist"):
        tool.params.model_validate(
            {
                "period": "2026-06",
                "allocations": [
                    {"artist_id": 1, "net_payable": "1"},
                    {"artist_id": 1, "net_payable": "2"},
                ],
                "flags": [],
            }
        )
    with pytest.raises(ValueError):
        tool.params.model_validate(
            {
                "period": "2026-06",
                "allocations": [{"artist_id": 1, "net_payable": "1"}],
                "flags": [{"kind": "x", "severity": "catastrophic", "payload": {}}],
            }
        )


async def test_staged_lines_flow_into_ledger_mode(
    emitted: str, ctx: ToolContext, pool: asyncpg.Pool
) -> None:
    ingest = build_ingest_statement_tool(ctx)
    await ingest.handler(ingest.params(path=f"data/inbox/kinetic_digital_{emitted}.csv"))
    artist_id = await pool.fetchval(
        """
        SELECT t.primary_artist_id FROM staging.ingested_lines il
        JOIN label.tracks t ON t.isrc = il.isrc
        WHERE il.period = $1 AND il.units > 0
        GROUP BY t.primary_artist_id ORDER BY count(*) DESC LIMIT 1
        """,
        emitted,
    )
    slice_ = await compute_ledger_slice(
        pool, artist_id=artist_id, period=emitted, include_staged=True
    )
    assert slice_.n_staged_used > 0
    assert slice_.gross > 0
