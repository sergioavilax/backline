"""The embedding build job behind ``make embed`` (§4.4). Idempotent, hash-keyed.

Three steps, each safe to repeat:

1. **Chunk** — parse every contract's ``.txt`` sidecar into clause chunks and reconcile
   them against ``rag.contract_chunks`` by content hash: unchanged rows are untouched
   (embeddings intact), changed/new rows upsert with ``embedding = NULL``, stale rows
   are deleted. Chunking needs no model, so FTS search works even fully offline.
2. **Embed** — encode rows where the embedding is missing *or* was produced by a
   different embedder (the store is single-model by construction; switching models
   re-embeds everything). Skipped when no embedder is available and the caller said
   ``--best-effort`` — loudly, never silently.
3. **Index** — (re)build the ivfflat cosine index *after* bulk embedding and ANALYZE,
   per the §9 pitfall (an ivfflat trained on an empty table has useless centroids).

CLI: ``python -m backline.rag.embed`` (compose init runs it with ``--best-effort`` so
a cold boot without model egress still yields a working FTS-only stack).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import asyncpg

from backline.config import get_settings
from backline.rag.chunker import ClauseChunk, chunk_document
from backline.rag.embedder import Embedder, get_embedder, vector_literal

_EMBED_BATCH = 128
_IVFFLAT_LISTS = 64  # ~sqrt(n_chunks) for this corpus (~3.5K chunks)


@dataclass(frozen=True)
class EmbedReport:
    contracts: int
    chunks_total: int
    chunks_inserted: int
    chunks_updated: int
    chunks_deleted: int
    embedded_now: int
    embedder_id: str | None
    elapsed_s: float


def sidecar_path(data_dir: Path, doc_path: str) -> Path:
    """``label.contracts.doc_path`` ('data/contracts/X.pdf') → the .txt sidecar on disk."""
    stem = Path(doc_path).stem
    return data_dir / "contracts" / "txt" / f"{stem}.txt"


async def _reconcile_chunks(
    conn: asyncpg.Connection, data_dir: Path
) -> tuple[int, int, int, int, int]:
    contracts = await conn.fetch(
        "SELECT id, artist_id, doc_path, effective_from, effective_to, kind "
        "FROM label.contracts ORDER BY id"
    )
    existing = {
        (r["contract_id"], r["clause_no"], r["part"]): r["content_hash"]
        for r in await conn.fetch(
            "SELECT contract_id, clause_no, part, content_hash FROM rag.contract_chunks"
        )
    }

    inserted = updated = 0
    seen: set[tuple[int, str, int]] = set()
    upserts: list[tuple[int, str, int, str, str, str, int, str, object, object]] = []
    for contract in contracts:
        path = sidecar_path(data_dir, contract["doc_path"])
        if not path.is_file():
            raise FileNotFoundError(
                f"contract sidecar missing: {path} — run `make seed` first (DATA_DIR must "
                f"point at the seeded corpus)"
            )
        chunks: list[ClauseChunk] = chunk_document(path.read_text(encoding="utf-8"))
        for chunk in chunks:
            key = (contract["id"], chunk.clause_no, chunk.part)
            seen.add(key)
            stored_hash = existing.get(key)
            if stored_hash == chunk.content_hash:
                continue
            if stored_hash is None:
                inserted += 1
            else:
                updated += 1
            upserts.append(
                (
                    contract["id"],
                    chunk.clause_no,
                    chunk.part,
                    chunk.heading,
                    chunk.content,
                    chunk.content_hash,
                    contract["artist_id"],
                    contract["kind"],
                    contract["effective_from"],
                    contract["effective_to"],
                )
            )

    if upserts:
        await conn.executemany(
            """
            INSERT INTO rag.contract_chunks
                (contract_id, clause_no, part, heading, content, content_hash,
                 artist_id, kind, effective_from, effective_to)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
            ON CONFLICT (contract_id, clause_no, part) DO UPDATE SET
                heading = EXCLUDED.heading,
                content = EXCLUDED.content,
                content_hash = EXCLUDED.content_hash,
                artist_id = EXCLUDED.artist_id,
                kind = EXCLUDED.kind,
                effective_from = EXCLUDED.effective_from,
                effective_to = EXCLUDED.effective_to,
                embedding = NULL,
                embedding_model = NULL
            """,
            upserts,
        )

    stale = [key for key in existing if key not in seen]
    for contract_id, clause_no, part in stale:
        await conn.execute(
            "DELETE FROM rag.contract_chunks "
            "WHERE contract_id = $1 AND clause_no = $2 AND part = $3",
            contract_id,
            clause_no,
            part,
        )

    total = len(seen)
    return len(contracts), total, inserted, updated, len(stale)


async def _embed_missing(conn: asyncpg.Connection, embedder: Embedder) -> int:
    embedded = 0
    while True:
        rows = await conn.fetch(
            """
            SELECT contract_id, clause_no, part, heading, content
            FROM rag.contract_chunks
            WHERE embedding IS NULL OR embedding_model IS DISTINCT FROM $1
            ORDER BY contract_id, clause_no, part
            LIMIT $2
            """,
            embedder.id,
            _EMBED_BATCH,
        )
        if not rows:
            return embedded
        # Heading + body is what FTS ranks on; embed the same text.
        vectors = embedder.encode_passages([f"{r['heading']}\n{r['content']}" for r in rows])
        await conn.executemany(
            """
            UPDATE rag.contract_chunks
            SET embedding = $4::vector, embedding_model = $5
            WHERE contract_id = $1 AND clause_no = $2 AND part = $3
            """,
            [
                (r["contract_id"], r["clause_no"], r["part"], vector_literal(v), embedder.id)
                for r, v in zip(rows, vectors, strict=True)
            ],
        )
        embedded += len(rows)


async def _finalize_index(conn: asyncpg.Connection, *, rebuilt: bool) -> None:
    any_embedded = await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM rag.contract_chunks WHERE embedding IS NOT NULL)"
    )
    if not any_embedded:
        return
    if rebuilt:  # fresh vectors → retrain centroids
        await conn.execute("DROP INDEX IF EXISTS rag.contract_chunks_embedding_idx")
    await conn.execute(
        f"CREATE INDEX IF NOT EXISTS contract_chunks_embedding_idx "
        f"ON rag.contract_chunks USING ivfflat (embedding vector_cosine_ops) "
        f"WITH (lists = {_IVFFLAT_LISTS})"
    )
    await conn.execute("ANALYZE rag.contract_chunks")


async def run_embed(
    pool: asyncpg.Pool, *, data_dir: Path, embedder: Embedder | None
) -> EmbedReport:
    """Run the full job. ``embedder=None`` builds/reconciles chunks only."""
    started = time.perf_counter()
    async with pool.acquire() as conn:
        contracts, total, inserted, updated, deleted = await _reconcile_chunks(conn, data_dir)
        embedded_now = 0
        if embedder is not None:
            embedded_now = await _embed_missing(conn, embedder)
        await _finalize_index(conn, rebuilt=embedded_now > 0)
    return EmbedReport(
        contracts=contracts,
        chunks_total=total,
        chunks_inserted=inserted,
        chunks_updated=updated,
        chunks_deleted=deleted,
        embedded_now=embedded_now,
        embedder_id=None if embedder is None else embedder.id,
        elapsed_s=time.perf_counter() - started,
    )


async def _amain(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="backline.rag.embed", description="Build clause chunks + embeddings (idempotent)."
    )
    parser.add_argument(
        "--best-effort",
        action="store_true",
        help="if the embedding model is unavailable (no 'embed' extra / no model "
        "download), still build chunks (FTS works) and exit 0 with a loud warning",
    )
    parser.add_argument(
        "--embedder",
        default=None,
        help="override EMBED_MODEL ('hash' = deterministic offline embedder)",
    )
    args = parser.parse_args(argv)

    settings = get_settings()
    spec = args.embedder or settings.embed_model
    embedder: Embedder | None
    try:
        embedder = get_embedder(spec)
    except (RuntimeError, OSError) as error:
        if not args.best_effort:
            print(f"embed: cannot load embedder {spec!r}: {error}", file=sys.stderr)
            return 1
        embedder = None
        print(
            f"embed: WARNING — embedder {spec!r} unavailable ({error}); building chunks "
            f"only. Hybrid search runs FTS-only until `make embed` succeeds with a model.",
            file=sys.stderr,
        )

    pool = await asyncpg.create_pool(settings.database_url, min_size=1, max_size=4)
    try:
        report = await run_embed(pool, data_dir=Path(settings.data_dir), embedder=embedder)
    finally:
        await pool.close()
    print(
        f"embed: {report.contracts} contracts → {report.chunks_total} chunks "
        f"({report.chunks_inserted} new, {report.chunks_updated} changed, "
        f"{report.chunks_deleted} removed) · {report.embedded_now} embedded now "
        f"({report.embedder_id or 'no embedder'}) in {report.elapsed_s:.1f}s"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(_amain(argv))


if __name__ == "__main__":
    sys.exit(main())
