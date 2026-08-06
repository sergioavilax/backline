"""Review Queue: the human half of invariant 5 (§6 surface 3).

Agents stop at ``staging.statement_batches(status='proposed')``; everything that
moves state from there happens here, and only here:

- **approve** promotes (D-025): batch → ``approved`` (reviewer note + timestamp in
  ``summary.review``); the period's staged lines copy into ``label.statement_lines``
  and their statements flip ``received → ingested``. Promoted line ids continue from
  ``max(id)`` — datagen only writes label lines during a full (truncating) reseed,
  so the sequence cannot collide with a future seed.
- **reject** requires a note (the schema enforces it) and leaves staged lines in
  place for a corrected batch.

Both transitions are guarded ``WHERE status = 'proposed'`` inside the promoting
transaction — a batch reviewed twice concurrently resolves to exactly one winner and
the loser gets 409.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Any, Literal

import asyncpg
from fastapi import APIRouter, Depends, HTTPException

from backline.api.schemas import (
    AllocationOut,
    BatchDetailOut,
    BatchOut,
    FlagOut,
    PromotionPreviewOut,
    RejectIn,
    ReviewActionIn,
)
from backline.api.state import AppState, get_state, jload
from backline.jsonutil import canonical_dumps

router = APIRouter(prefix="/review", tags=["review"])

State = Annotated[AppState, Depends(get_state)]

_BATCH_COLUMNS = """
    b.id, b.period, b.status, b.submitted_by_run, b.summary, b.created_at,
    (SELECT count(*) FROM staging.proposed_allocations a WHERE a.batch_id = b.id)
        AS n_allocations,
    (SELECT count(*) FROM staging.flags f WHERE f.batch_id = b.id) AS n_flags,
    (SELECT COALESCE(sum(a.net_payable), 0) FROM staging.proposed_allocations a
        WHERE a.batch_id = b.id) AS total_net_payable
"""


def _batch_out(row: asyncpg.Record) -> BatchOut:
    return BatchOut(**{**dict(row), "summary": jload(row["summary"])})


async def _fetch_batch(pool: asyncpg.Pool, batch_id: int) -> asyncpg.Record:
    row = await pool.fetchrow(
        f"SELECT {_BATCH_COLUMNS} FROM staging.statement_batches b WHERE b.id = $1", batch_id
    )
    if row is None:
        raise HTTPException(status_code=404, detail=f"no batch {batch_id}")
    return row


@router.get("/batches", response_model=list[BatchOut])
async def list_batches(
    state: State,
    status: Literal["proposed", "approved", "rejected", "all"] = "proposed",
    limit: int = 50,
) -> list[BatchOut]:
    where = "TRUE" if status == "all" else "b.status = $1"
    args: list[Any] = [] if status == "all" else [status]
    args.append(min(limit, 200))
    rows = await state.pool.fetch(
        f"SELECT {_BATCH_COLUMNS} FROM staging.statement_batches b WHERE {where} "
        f"ORDER BY b.created_at DESC, b.id DESC LIMIT ${len(args)}",
        *args,
    )
    return [_batch_out(r) for r in rows]


async def _promotion_preview(pool: asyncpg.Pool, period: str) -> PromotionPreviewOut:
    """What approval would move from staging into label for this period."""
    statements = await pool.fetch(
        """
        SELECT s.id, d.name AS distributor, s.raw_path, s.status,
               count(i.id) AS n_staged_lines
        FROM label.statements s
        JOIN label.distributors d ON d.id = s.distributor_id
        JOIN staging.ingested_lines i ON i.statement_id = s.id
        WHERE s.period = $1 AND s.status = 'received'
        GROUP BY s.id, d.name, s.raw_path, s.status
        ORDER BY s.id
        """,
        period,
    )
    by_currency = await pool.fetch(
        """
        SELECT i.currency, sum(i.gross_amount) AS gross
        FROM staging.ingested_lines i
        JOIN label.statements s ON s.id = i.statement_id
        WHERE s.period = $1 AND s.status = 'received'
        GROUP BY i.currency ORDER BY i.currency
        """,
        period,
    )
    return PromotionPreviewOut(
        statements_to_promote=[dict(r) for r in statements],
        n_staged_lines=sum(r["n_staged_lines"] for r in statements),
        staged_gross_by_currency={r["currency"]: r["gross"] for r in by_currency},
        allocation_total=Decimal("0"),  # filled by the caller from the batch total
        n_paid_artists=0,
    )


_EVIDENCE_SHOWN = 5


async def _flag_evidence(
    pool: asyncpg.Pool, flags: list[asyncpg.Record]
) -> dict[int, list[dict[str, Any]]]:
    """Resolve the statement lines a flag's payload points at (linked evidence)."""
    label_ids: set[int] = set()
    staged_ids: set[int] = set()
    for flag in flags:
        payload = jload(flag["payload"]) or {}
        line_id = payload.get("line_id")
        if isinstance(line_id, int):
            (staged_ids if payload.get("source") == "staged" else label_ids).add(line_id)
        for extra in payload.get("line_ids", [])[:_EVIDENCE_SHOWN]:
            if isinstance(extra, int):
                (staged_ids if payload.get("source") == "staged" else label_ids).add(extra)

    lines: dict[tuple[str, int], dict[str, Any]] = {}
    if label_ids:
        for row in await pool.fetch(
            "SELECT id, statement_id, period, isrc, upc, store, territory, units, "
            "gross_amount, currency FROM label.statement_lines WHERE id = ANY($1::bigint[])",
            list(label_ids),
        ):
            lines[("label", row["id"])] = {"source": "label", **dict(row)}
    if staged_ids:
        for row in await pool.fetch(
            "SELECT id, statement_id, period, isrc, upc, store, territory, units, "
            "gross_amount, currency FROM staging.ingested_lines WHERE id = ANY($1::bigint[])",
            list(staged_ids),
        ):
            lines[("staged", row["id"])] = {"source": "staged", **dict(row)}

    evidence: dict[int, list[dict[str, Any]]] = {}
    for flag in flags:
        payload = jload(flag["payload"]) or {}
        source = "staged" if payload.get("source") == "staged" else "label"
        ids = [payload.get("line_id"), *payload.get("line_ids", [])]
        rows = [lines[(source, i)] for i in ids if isinstance(i, int) and (source, i) in lines]
        evidence[flag["id"]] = rows[:_EVIDENCE_SHOWN]
    return evidence


@router.get("/batches/{batch_id}", response_model=BatchDetailOut)
async def get_batch(batch_id: int, state: State) -> BatchDetailOut:
    row = await _fetch_batch(state.pool, batch_id)
    allocations = await state.pool.fetch(
        """
        SELECT a.artist_id, ar.stage_name, a.period, a.net_payable, a.line_detail
        FROM staging.proposed_allocations a
        LEFT JOIN label.artists ar ON ar.id = a.artist_id
        WHERE a.batch_id = $1 ORDER BY a.net_payable DESC, a.artist_id
        """,
        batch_id,
    )
    flags = await state.pool.fetch(
        "SELECT id, kind, severity, payload FROM staging.flags WHERE batch_id = $1 "
        "ORDER BY CASE severity WHEN 'error' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END, id",
        batch_id,
    )
    evidence = await _flag_evidence(state.pool, list(flags))
    promotion = await _promotion_preview(state.pool, row["period"])
    promotion = promotion.model_copy(
        update={
            "allocation_total": row["total_net_payable"],
            "n_paid_artists": row["n_allocations"],
        }
    )
    return BatchDetailOut(
        batch=_batch_out(row),
        allocations=[
            AllocationOut(**{**dict(a), "line_detail": jload(a["line_detail"])})
            for a in allocations
        ],
        flags=[
            FlagOut(
                **{**dict(f), "payload": jload(f["payload"])},
                evidence=evidence.get(f["id"], []),
            )
            for f in flags
        ],
        promotion=promotion,
    )


async def _transition(
    state: State,
    batch_id: int,
    *,
    to_status: Literal["approved", "rejected"],
    note: str,
) -> BatchOut:
    row = await _fetch_batch(state.pool, batch_id)
    if row["status"] != "proposed":
        raise HTTPException(
            status_code=409,
            detail=f"batch {batch_id} is already {row['status']} — only proposed "
            f"batches can be reviewed",
        )
    review = {"action": to_status, "note": note}
    async with state.pool.acquire() as conn, conn.transaction():
        updated = await conn.fetchrow(
            "UPDATE staging.statement_batches SET status = $2, "
            "summary = summary || jsonb_build_object('review', "
            "  $3::jsonb || jsonb_build_object('at', now()::text)) "
            "WHERE id = $1 AND status = 'proposed' RETURNING id",
            batch_id,
            to_status,
            canonical_dumps(review),
        )
        if updated is None:  # lost a concurrent review race
            raise HTTPException(status_code=409, detail=f"batch {batch_id} was just reviewed")
        if to_status == "approved":
            await _promote(conn, row["period"])
    return _batch_out(await _fetch_batch(state.pool, batch_id))


async def _promote(conn: asyncpg.Connection, period: str) -> None:
    """D-025: staged lines for the period's received statements become label lines."""
    await conn.execute(
        """
        INSERT INTO label.statement_lines
            (id, statement_id, period, isrc, upc, store, territory, units,
             gross_amount, currency, line_hash)
        SELECT (SELECT COALESCE(max(id), 0) FROM label.statement_lines)
                   + row_number() OVER (ORDER BY i.id),
               i.statement_id, i.period, i.isrc, i.upc, i.store, i.territory,
               i.units, i.gross_amount, i.currency, i.line_hash
        FROM staging.ingested_lines i
        JOIN label.statements s ON s.id = i.statement_id
        WHERE s.period = $1 AND s.status = 'received'
        """,
        period,
    )
    await conn.execute(
        """
        UPDATE label.statements s SET status = 'ingested'
        WHERE s.period = $1 AND s.status = 'received'
          AND EXISTS (SELECT 1 FROM staging.ingested_lines i WHERE i.statement_id = s.id)
        """,
        period,
    )
    await conn.execute(
        """
        DELETE FROM staging.ingested_lines i
        USING label.statements s
        WHERE s.id = i.statement_id AND s.period = $1 AND s.status = 'ingested'
        """,
        period,
    )


@router.post("/batches/{batch_id}/approve", response_model=BatchOut)
async def approve_batch(batch_id: int, body: ReviewActionIn, state: State) -> BatchOut:
    return await _transition(state, batch_id, to_status="approved", note=body.note)


@router.post("/batches/{batch_id}/reject", response_model=BatchOut)
async def reject_batch(batch_id: int, body: RejectIn, state: State) -> BatchOut:
    """Reject a proposed batch. A note is required — 'no' with no reason is not a
    review."""
    return await _transition(state, batch_id, to_status="rejected", note=body.note)
