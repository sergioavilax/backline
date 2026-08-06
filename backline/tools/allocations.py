"""``compute_allocations`` — period allocations for the Reconciler batch (Phase 4).

One call computes proposed net payables for every artist with reported revenue in a
period, through the same ``compute_ledger_slice`` every other calculation uses
(D-001: one royalty engine). The per-artist chains run with bounded concurrency;
exclusions are per source (label vs staged ids — separate sequences). The result is
the material the agent copies into ``submit_batch`` verbatim: per-artist net payable
plus the gross/recouped/balance detail for ``line_detail``.

Materiality: ``min_net_payable`` (default $0.01) keeps zero-payable artists —
typically the unrecouped bulk of the roster — out of the allocation list; they are
counted and reported instead, so the reviewer sees coverage, not silence.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from backline.core.runtime import Tool
from backline.tools.context import ToolContext
from backline.tools.ledger import CalcInputError, LedgerSlice, compute_ledger_slice

_CONCURRENCY = 4  # matches the default pool size; chains are query-heavy


class ComputeAllocationsParams(BaseModel):
    model_config = ConfigDict(frozen=True)

    period: str = Field(pattern=r"^\d{4}-\d{2}$", description="the period to allocate")
    include_staged: bool = Field(
        default=False,
        description="include lines staged via ingest_statement (the fresh-drop workflow)",
    )
    exclude_line_ids: list[int] = Field(
        default_factory=list,
        description="label statement line ids to exclude (accepted anomaly candidates)",
    )
    exclude_staged_line_ids: list[int] = Field(
        default_factory=list,
        description="staged (staging.ingested_lines) ids to exclude — a separate id "
        "sequence from label lines",
    )
    min_net_payable: Decimal = Field(
        default=Decimal("0.01"),
        ge=0,
        description="materiality floor in USD: artists below it are aggregated, not listed",
    )


async def _artists_with_revenue(ctx: ToolContext, period: str, include_staged: bool) -> list[int]:
    """Artists with reported lines in the period (ISRC → track, or UPC → single-artist
    release for blank-ISRC physical) — the allocation candidates."""
    sql = """
    WITH period_lines AS (
        SELECT isrc, upc FROM label.statement_lines WHERE period = $1
        {staged}
    )
    SELECT DISTINCT artist_id FROM (
        SELECT t.primary_artist_id AS artist_id
        FROM period_lines l JOIN label.tracks t ON l.isrc <> '' AND t.isrc = l.isrc
        UNION
        SELECT (
            SELECT MIN(t2.primary_artist_id)
            FROM label.releases rel
            JOIN label.release_tracks rt ON rt.release_id = rel.id
            JOIN label.tracks t2 ON t2.id = rt.track_id
            WHERE rel.upc = l.upc
            HAVING COUNT(DISTINCT t2.primary_artist_id) = 1
        )
        FROM period_lines l WHERE l.isrc = '' AND l.upc IS NOT NULL
    ) matched
    WHERE artist_id IS NOT NULL
    ORDER BY artist_id
    """
    staged_clause = (
        "UNION ALL SELECT isrc, upc FROM staging.ingested_lines WHERE period = $1"
        if include_staged
        else ""
    )
    rows = await ctx.pool.fetch(sql.format(staged=staged_clause), period)
    return [row["artist_id"] for row in rows]


def build_compute_allocations_tool(ctx: ToolContext) -> Tool[ComputeAllocationsParams]:
    async def handler(params: ComputeAllocationsParams) -> str:
        artist_ids = await _artists_with_revenue(ctx, params.period, params.include_staged)
        if not artist_ids:
            return (
                f"No artists have reported lines for {params.period}"
                + ("" if params.include_staged else " (staged lines not included)")
                + ". Ingest the period's drops first, or check the period."
            )

        semaphore = asyncio.Semaphore(_CONCURRENCY)
        errors: dict[int, str] = {}

        async def one(artist_id: int) -> LedgerSlice | None:
            async with semaphore:
                try:
                    return await compute_ledger_slice(
                        ctx.pool,
                        artist_id=artist_id,
                        period=params.period,
                        exclude_line_ids=tuple(params.exclude_line_ids),
                        exclude_staged_line_ids=tuple(params.exclude_staged_line_ids),
                        include_staged=params.include_staged,
                    )
                except CalcInputError as error:
                    errors[artist_id] = str(error)
                    return None

        slices = [s for s in await asyncio.gather(*(one(a) for a in artist_ids)) if s is not None]
        names = {
            row["id"]: row["stage_name"]
            for row in await ctx.pool.fetch(
                "SELECT id, stage_name FROM label.artists WHERE id = ANY($1::bigint[])",
                artist_ids,
            )
        }

        floor = params.min_net_payable
        payable = sorted(
            (s for s in slices if s.net_payable >= floor and s.net_payable > 0),
            key=lambda s: (-s.net_payable, s.artist_id),
        )
        payable_ids = {s.artist_id for s in payable}
        below = [s for s in slices if s.artist_id not in payable_ids]
        below_total = sum((s.net_payable for s in below), Decimal("0"))
        n_zero = sum(1 for s in below if s.net_payable == 0)
        total = sum((s.net_payable for s in payable), Decimal("0"))

        excluded_label = sorted({i for s in slices for i in s.excluded_line_ids})
        excluded_staged = sorted({i for s in slices for i in s.excluded_staged_line_ids})
        auto_excluded = sorted({i for s in slices for i in s.auto_excluded_line_ids})
        staged_used = sum(s.n_staged_used for s in slices)

        out = [
            f"Proposed allocations — period {params.period}, {len(slices)} artists "
            f"computed via royaltycalc"
            + (f" ({staged_used} staged lines in play)" if staged_used else "")
            + ":",
            "",
            f"{len(payable)} artists at or above {floor} USD net payable "
            f"(total {total} USD), listed for submit_batch:",
        ]
        out.extend(
            f"  artist {s.artist_id} ({names.get(s.artist_id, '?')}): "
            f"net_payable {s.net_payable} · gross {s.gross} · recouped {s.recouped} · "
            f"balance_after {s.balance_after}"
            for s in payable
        )
        out.append(
            f"{len(below)} artists below the floor ({n_zero} at exactly 0.00, typically "
            f"unrecouped) aggregating {below_total} USD — report this coverage in the "
            f"batch note."
        )
        if errors:
            out.append(
                f"{len(errors)} artist(s) not computable: "
                + "; ".join(f"artist {a}: {msg}" for a, msg in sorted(errors.items()))
            )
        if excluded_label:
            out.append(f"Excluded label lines honored: {excluded_label}")
        if excluded_staged:
            out.append(f"Excluded staged lines honored: {excluded_staged}")
        if auto_excluded:
            out.append(
                f"Auto-excluded (negative/zero units or negative amounts, engine "
                f"refuses): {auto_excluded}"
            )
        out.append(
            "Use these rows verbatim in submit_batch allocations "
            "(net_payable as given; gross/recouped/balance_after go in line_detail)."
        )
        return "\n".join(out)

    return Tool(
        name="compute_allocations",
        description=(
            "Compute proposed per-artist net payables for a whole period through the "
            "royalty engine (rate cards, FX, escalators, recoupment, minimum "
            "guarantees, cross-collateral pooling) — the allocation step of "
            "reconciliation. Honors per-source line exclusions and staged lines; "
            "applies a materiality floor and reports coverage below it. Feed the "
            "returned rows to submit_batch verbatim."
        ),
        params=ComputeAllocationsParams,
        handler=handler,
    )
