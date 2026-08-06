"""Reconciler workflow tools (§4.3): ingest → match → (calc) → submit for review.

Invariant 5 end to end: ``ingest_statement`` parses a received drop into
``staging.ingested_lines`` — ``label.*`` is never touched by an agent, and the
statement stays ``received`` until a human approves the batch (promotion is the
Phase 6 review action). ``match_lines`` partitions staged/ingested lines against the
catalog. ``submit_batch`` is the **only write path an agent has** toward money moving:
it inserts a ``proposed`` batch + allocations + flags, stamped with the proposing run,
and nothing in this codebase lets an agent change a batch's status.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

import asyncpg
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from backline.core.runcontext import current_run_id
from backline.core.runtime import Tool
from backline.jsonutil import canonical_dumps
from backline.royaltycalc import money6
from backline.tools.context import ToolContext
from backline.tools.normalizer import parse_drop

_UNMATCHED_SHOWN = 25
_ERRORS_SHOWN = 10


class IngestStatementParams(BaseModel):
    model_config = ConfigDict(frozen=True)

    path: str = Field(
        description="the drop to ingest, e.g. data/inbox/kinetic_digital_2026-07.csv "
        "(as listed in label.statements.raw_path)"
    )


class MatchLinesParams(BaseModel):
    model_config = ConfigDict(frozen=True)

    statement_id: int = Field(description="id from label.statements")


class Allocation(BaseModel):
    model_config = ConfigDict(frozen=True)

    artist_id: int
    net_payable: Decimal = Field(description="proposed net payable for the period, USD")
    line_detail: dict[str, Any] = Field(default_factory=dict)


class BatchFlag(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: str = Field(
        min_length=1,
        max_length=64,
        description="e.g. duplicate_line, unknown_isrc, currency_mismatch",
    )
    severity: Literal["info", "warning", "error"]
    payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("kind")
    @classmethod
    def _kind_shape(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("flag kind must not be blank")
        return value.strip()


class SubmitBatchParams(BaseModel):
    model_config = ConfigDict(frozen=True)

    period: str = Field(pattern=r"^\d{4}-\d{2}$")
    allocations: list[Allocation]
    flags: list[BatchFlag] = Field(default_factory=list)
    note: str = Field(default="", description="short context for the human reviewer")

    @model_validator(mode="after")
    def _distinct_artists(self) -> SubmitBatchParams:
        seen: set[int] = set()
        for allocation in self.allocations:
            if allocation.artist_id in seen:
                raise ValueError(
                    f"duplicate allocation for artist {allocation.artist_id} — one "
                    f"allocation per artist per batch"
                )
            seen.add(allocation.artist_id)
        return self


async def _statement_by_path(conn: asyncpg.Pool | asyncpg.Connection, path: str) -> Any:
    filename = Path(path).name
    return await conn.fetchrow(
        """
        SELECT s.id, s.period, s.status, s.raw_path, d.dialect, d.name AS feed_name
        FROM label.statements s
        JOIN label.distributors d ON d.id = s.distributor_id
        WHERE s.raw_path LIKE '%' || $1
        """,
        filename,
    )


def build_ingest_statement_tool(ctx: ToolContext) -> Tool[IngestStatementParams]:
    async def handler(params: IngestStatementParams) -> str:
        statement = await _statement_by_path(ctx.pool, params.path)
        if statement is None:
            return (
                f"No statement recorded for drop {Path(params.path).name!r} — check "
                f"data/inbox and label.statements.raw_path for the exact filename."
            )
        if statement["status"] == "ingested":
            return (
                f"Statement {statement['id']} ({statement['feed_name']}, period "
                f"{statement['period']}) is already ingested — its lines are in "
                f"label.statement_lines. ingest_statement is only for 'received' drops."
            )
        file_path = ctx.settings.data_path / "inbox" / Path(params.path).name
        if not file_path.is_file():
            return (
                f"Statement {statement['id']} expects the drop at {statement['raw_path']}, "
                f"but {file_path} does not exist on disk."
            )
        lines, errors = parse_drop(statement["dialect"], file_path.read_text("utf-8"))

        run_id = current_run_id.get()
        async with ctx.pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "DELETE FROM staging.ingested_lines WHERE statement_id = $1",
                statement["id"],
            )
            await conn.executemany(
                """
                INSERT INTO staging.ingested_lines
                    (statement_id, period, isrc, upc, store, territory, units,
                     gross_amount, currency, line_hash, ingested_by_run)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                """,
                [
                    (
                        statement["id"],
                        ln.period,
                        ln.isrc,
                        ln.upc,
                        ln.store,
                        ln.territory,
                        ln.units,
                        ln.gross_amount,
                        ln.currency,
                        ln.line_hash,
                        run_id,
                    )
                    for ln in lines
                ],
            )

        by_currency: dict[str, tuple[int, Decimal]] = {}
        n_negative = n_blank_isrc = n_off_period = 0
        hash_counts: dict[str, int] = {}
        for ln in lines:
            n, total = by_currency.get(ln.currency, (0, Decimal("0")))
            by_currency[ln.currency] = (n + 1, total + ln.gross_amount)
            if ln.units < 0:
                n_negative += 1
            if not ln.isrc:
                n_blank_isrc += 1
            if ln.period != statement["period"]:
                n_off_period += 1
            hash_counts[ln.line_hash] = hash_counts.get(ln.line_hash, 0) + 1
        n_dup_content = sum(count - 1 for count in hash_counts.values() if count > 1)

        report = [
            f"Parsed {statement['feed_name']} drop ({statement['dialect']}) — "
            f"statement {statement['id']}, period {statement['period']}.",
            f"{len(lines)} lines staged into staging.ingested_lines (replaced any "
            f"previous staging for this statement).",
            "By currency: "
            + " · ".join(
                f"{ccy}: {n} lines, {total}" for ccy, (n, total) in sorted(by_currency.items())
            ),
        ]
        signals = []
        if n_dup_content:
            signals.append(f"{n_dup_content} exact-duplicate line(s) (same content hash)")
        if n_negative:
            signals.append(f"{n_negative} negative-unit line(s)")
        if n_off_period:
            signals.append(f"{n_off_period} line(s) dated outside the statement period")
        if n_blank_isrc:
            signals.append(f"{n_blank_isrc} blank-ISRC line(s) (physical; match by UPC)")
        if signals:
            report.append("Signals worth checking: " + "; ".join(signals) + ".")
        if errors:
            shown = errors[:_ERRORS_SHOWN]
            report.append(
                f"{len(errors)} row(s) failed to parse and were NOT staged: "
                + " | ".join(shown)
                + (" | …" if len(errors) > _ERRORS_SHOWN else "")
            )
        report.append(
            "Statement remains status='received'. Next: match_lines(statement_id="
            f"{statement['id']}), then calc_royalties(..., include_staged=true); "
            "nothing posts to label until a human approves the submitted batch."
        )
        return "\n".join(report)

    return Tool(
        name="ingest_statement",
        description=(
            "Parse a received distributor drop from data/inbox through the feed-dialect "
            "normalizer into staging.ingested_lines (replaces any earlier staging for "
            "that statement; never writes to label.*). Returns a parse report with "
            "per-currency totals and anomaly signals (duplicates, negative units, "
            "off-period lines, parse failures)."
        ),
        params=IngestStatementParams,
        handler=handler,
    )


def build_match_lines_tool(ctx: ToolContext) -> Tool[MatchLinesParams]:
    async def handler(params: MatchLinesParams) -> str:
        statement = await ctx.pool.fetchrow(
            """
            SELECT s.id, s.period, s.status, s.raw_path, d.name AS feed_name
            FROM label.statements s JOIN label.distributors d ON d.id = s.distributor_id
            WHERE s.id = $1
            """,
            params.statement_id,
        )
        if statement is None:
            return f"No statement with id {params.statement_id} in label.statements."
        if statement["status"] == "ingested":
            source = "label.statement_lines"
        else:
            staged = await ctx.pool.fetchval(
                "SELECT count(*) FROM staging.ingested_lines WHERE statement_id = $1",
                params.statement_id,
            )
            if not staged:
                return (
                    f"Statement {statement['id']} ({statement['feed_name']}, period "
                    f"{statement['period']}) is 'received' but nothing is staged — run "
                    f"ingest_statement(path={statement['raw_path']!r}) first."
                )
            source = "staging.ingested_lines"

        rows = await ctx.pool.fetch(
            f"""
            SELECT l.id, l.isrc, l.upc, l.store, l.territory, l.units, l.gross_amount,
                   l.currency, COALESCE(t.primary_artist_id, ru.artist_id) AS artist_id
            FROM {source} l
            LEFT JOIN label.tracks t ON l.isrc <> '' AND t.isrc = l.isrc
            LEFT JOIN LATERAL (
                SELECT MIN(t2.primary_artist_id) AS artist_id
                FROM label.releases rel
                JOIN label.release_tracks rt ON rt.release_id = rel.id
                JOIN label.tracks t2 ON t2.id = rt.track_id
                WHERE rel.upc = l.upc
                HAVING COUNT(DISTINCT t2.primary_artist_id) = 1
            ) ru ON l.isrc = '' AND l.upc IS NOT NULL
            WHERE l.statement_id = $1
            ORDER BY l.id
            """,
            params.statement_id,
        )
        matched = [r for r in rows if r["artist_id"] is not None]
        unmatched = [r for r in rows if r["artist_id"] is None]
        artists = {r["artist_id"] for r in matched}

        report = [
            f"Statement {statement['id']} ({statement['feed_name']}, period "
            f"{statement['period']}, source {source}): {len(rows)} lines — "
            f"{len(matched)} matched to catalog across {len(artists)} artists, "
            f"{len(unmatched)} unmatched.",
        ]
        if unmatched:
            report.append(
                "Unmatched lines (no catalog ISRC/UPC — candidates for unknown_isrc flags):"
            )
            report.extend(
                f"  line {r['id']}: isrc={r['isrc'] or '∅'} upc={r['upc'] or '∅'} "
                f"{r['store']} {r['territory']} units={r['units']} "
                f"{r['gross_amount']} {r['currency']}"
                for r in unmatched[:_UNMATCHED_SHOWN]
            )
            if len(unmatched) > _UNMATCHED_SHOWN:
                report.append(f"  … and {len(unmatched) - _UNMATCHED_SHOWN} more")
        return "\n".join(report)

    return Tool(
        name="match_lines",
        description=(
            "Match a statement's lines to the catalog (ISRC → track; blank-ISRC physical "
            "lines by UPC → single-artist release). Works on ingested statements "
            "(label.statement_lines) and staged ones (staging.ingested_lines). Returns "
            "the matched/unmatched partition with unmatched line detail."
        ),
        params=MatchLinesParams,
        handler=handler,
    )


def build_submit_batch_tool(ctx: ToolContext) -> Tool[SubmitBatchParams]:
    async def handler(params: SubmitBatchParams) -> str:
        run_id = current_run_id.get()
        total = money6(sum((a.net_payable for a in params.allocations), Decimal("0")))
        severity_counts: dict[str, int] = {}
        for flag in params.flags:
            severity_counts[flag.severity] = severity_counts.get(flag.severity, 0) + 1
        summary = {
            "n_allocations": len(params.allocations),
            "total_net_payable": str(total),
            "flags_by_severity": severity_counts,
            "note": params.note,
        }
        async with ctx.pool.acquire() as conn, conn.transaction():
            batch_id = await conn.fetchval(
                "INSERT INTO staging.statement_batches (period, submitted_by_run, summary) "
                "VALUES ($1, $2, $3::jsonb) RETURNING id",
                params.period,
                run_id,
                canonical_dumps(summary),
            )
            await conn.executemany(
                "INSERT INTO staging.proposed_allocations "
                "(batch_id, artist_id, period, line_detail, net_payable) "
                "VALUES ($1, $2, $3, $4::jsonb, $5)",
                [
                    (
                        batch_id,
                        a.artist_id,
                        params.period,
                        canonical_dumps(a.line_detail),
                        money6(a.net_payable),
                    )
                    for a in params.allocations
                ],
            )
            await conn.executemany(
                "INSERT INTO staging.flags (batch_id, kind, severity, payload) "
                "VALUES ($1, $2, $3, $4::jsonb)",
                [(batch_id, f.kind, f.severity, canonical_dumps(f.payload)) for f in params.flags],
            )
        flag_text = (
            " · ".join(f"{sev}: {n}" for sev, n in sorted(severity_counts.items())) or "none"
        )
        return (
            f"Submitted batch {batch_id} for period {params.period}: "
            f"{len(params.allocations)} allocations totalling {total} USD net payable, "
            f"{len(params.flags)} flags ({flag_text}). Status: proposed — a human "
            f"reviews and approves it in the Review Queue; agents cannot approve, "
            f"reject, or promote batches."
        )

    return Tool(
        name="submit_batch",
        description=(
            "Submit a proposed statement batch for human review: per-artist net-payable "
            "allocations plus anomaly flags. Writes to staging only and returns the "
            "batch id. This is the only write path toward label state, and it stops at "
            "'proposed' — approval is a human action."
        ),
        params=SubmitBatchParams,
        handler=handler,
    )
