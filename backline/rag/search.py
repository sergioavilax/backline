"""Hybrid clause search (§4.4): governing filter → FTS + vector → RRF → rerank.

Order of operations is the design (D-002): the governing-document filter runs as SQL
*before* any ranking, so both legs only ever see clauses that can govern the question
(``include_history=True`` lifts that for explicitly historical questions). The two legs
are Postgres FTS (``ts_rank_cd`` over the weighted tsvector) and pgvector cosine over
the stored embeddings; Reciprocal Rank Fusion merges them; the cross-encoder reranks
only the fused top slice (§9: RRF before rerank, rerank on top-30 only).

The chunk store records which embedder produced it; queries must embed with the same
model — a mismatch raises instead of silently searching garbage. A store with no
embeddings at all (e.g. a cold boot that ran ``embed --best-effort`` offline) degrades
to FTS-only, recorded in the result's ``mode``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

import asyncpg

from backline.rag.embedder import Embedder, get_embedder, vector_literal
from backline.rag.governing import governing_docs
from backline.rag.reranker import Reranker

RRF_K = 60  # standard reciprocal-rank-fusion constant
CANDIDATES_PER_LEG = 50
RERANK_TOP = 30
IVFFLAT_PROBES = 16  # lists=64 on ~3.5K chunks; generous probes for recall

_CHUNK_COLUMNS = (
    "ch.contract_id, ch.clause_no, ch.part, ch.heading, ch.content, ch.artist_id, "
    "a.stage_name AS artist_name, ch.kind, ch.effective_from, ch.effective_to"
)


@dataclass(frozen=True)
class SearchHit:
    contract_id: int
    clause_no: str
    part: int
    heading: str
    content: str
    artist_id: int
    artist_name: str
    kind: str
    effective_from: date
    effective_to: date | None
    score: float
    fts_rank: int | None
    vec_rank: int | None


@dataclass(frozen=True)
class SearchResult:
    hits: list[SearchHit]
    mode: str  # "hybrid" | "fts-only"
    embedder_id: str | None
    reranked_by: str | None


ChunkKey = tuple[int, str, int]


def rrf_fuse(rankings: list[list[ChunkKey]], k: int = RRF_K) -> dict[ChunkKey, float]:
    """Reciprocal Rank Fusion: score(d) = Σ_legs 1 / (k + rank_leg(d))."""
    scores: dict[ChunkKey, float] = {}
    for ranking in rankings:
        for rank, key in enumerate(ranking, start=1):
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank)
    return scores


async def _stored_embedding_model(source: asyncpg.Pool | asyncpg.Connection) -> str | None:
    rows = await source.fetch(
        "SELECT DISTINCT embedding_model FROM rag.contract_chunks "
        "WHERE embedding IS NOT NULL LIMIT 2"
    )
    if not rows:
        return None
    if len(rows) > 1:
        raise RuntimeError(
            "rag.contract_chunks holds embeddings from more than one model — "
            "run `make embed` to rebuild a consistent store"
        )
    model = rows[0]["embedding_model"]
    return str(model) if model is not None else None


def _resolve_query_embedder(stored_model: str, provided: Embedder | None) -> Embedder:
    if provided is not None:
        if provided.id != stored_model:
            raise RuntimeError(
                f"query embedder {provided.id!r} does not match the chunk store's "
                f"embedding model {stored_model!r} — queries must embed with the model "
                f"that built the store (re-run `make embed` to switch)"
            )
        return provided
    # Process-wide cache: resolving the store's model must not reload weights per query.
    return get_embedder(stored_model)


async def search_chunks(
    source: asyncpg.Pool | asyncpg.Connection,
    query: str,
    *,
    artist_id: int | None = None,
    as_of: date | None = None,
    include_history: bool = False,
    top_k: int = 8,
    embedder: Embedder | None = None,
    reranker: Reranker | None = None,
) -> SearchResult:
    """Run the full pipeline; deterministic given the store and the (offline) stack."""
    as_of = as_of or date.today()

    filters = ["1 = 1"]
    params: list[Any] = []
    if include_history:
        if artist_id is not None:
            params.append(artist_id)
            filters.append(f"ch.artist_id = ${len(params)}")
    else:
        docs = await governing_docs(source, artist_id=artist_id, as_of=as_of)
        ids = [d.contract_id for d in docs]
        dead = [f"{d.contract_id}:{clause}" for d in docs for clause in d.excluded_clauses]
        params.append(ids)
        filters.append(f"ch.contract_id = ANY(${len(params)}::bigint[])")
        params.append(dead)
        filters.append(
            f"NOT (ch.contract_id::text || ':' || ch.clause_no = ANY(${len(params)}::text[]))"
        )
    where = " AND ".join(filters)

    stored_model = await _stored_embedding_model(source)

    fts_params = [*params, query]
    fts_rows = await source.fetch(
        f"""
        SELECT {_CHUNK_COLUMNS},
               ts_rank_cd(ch.tsv, websearch_to_tsquery('english', ${len(fts_params)})) AS r
        FROM rag.contract_chunks ch
        JOIN label.artists a ON a.id = ch.artist_id
        WHERE {where} AND ch.tsv @@ websearch_to_tsquery('english', ${len(fts_params)})
        ORDER BY r DESC, ch.contract_id, ch.clause_no, ch.part
        LIMIT {CANDIDATES_PER_LEG}
        """,
        *fts_params,
    )

    vec_rows: list[asyncpg.Record] = []
    embedder_id: str | None = None
    if stored_model is not None:
        active = _resolve_query_embedder(stored_model, embedder)
        embedder_id = active.id
        qvec = vector_literal(active.encode_query(query))
        vec_params = [*params, qvec]
        vec_sql = f"""
            SELECT {_CHUNK_COLUMNS},
                   ch.embedding <=> ${len(vec_params)}::vector AS dist
            FROM rag.contract_chunks ch
            JOIN label.artists a ON a.id = ch.artist_id
            WHERE {where} AND ch.embedding IS NOT NULL
            ORDER BY dist, ch.contract_id, ch.clause_no, ch.part
            LIMIT {CANDIDATES_PER_LEG}
            """
        if isinstance(source, asyncpg.Pool):
            async with source.acquire() as conn, conn.transaction():
                await conn.execute(f"SET LOCAL ivfflat.probes = {IVFFLAT_PROBES}")
                vec_rows = await conn.fetch(vec_sql, *vec_params)
        else:
            async with source.transaction():
                await source.execute(f"SET LOCAL ivfflat.probes = {IVFFLAT_PROBES}")
                vec_rows = await source.fetch(vec_sql, *vec_params)

    def key_of(row: asyncpg.Record) -> ChunkKey:
        return (row["contract_id"], row["clause_no"], row["part"])

    fts_ranking = [key_of(r) for r in fts_rows]
    vec_ranking = [key_of(r) for r in vec_rows]
    fused = rrf_fuse([ranking for ranking in (fts_ranking, vec_ranking) if ranking])
    rows_by_key = {key_of(r): r for r in [*fts_rows, *vec_rows]}
    ordered = sorted(fused.items(), key=lambda item: (-item[1], item[0]))[:RERANK_TOP]

    fts_rank_of = {key: i + 1 for i, key in enumerate(fts_ranking)}
    vec_rank_of = {key: i + 1 for i, key in enumerate(vec_ranking)}

    reranked_by: str | None = None
    if reranker is not None and ordered:
        texts = [
            f"{rows_by_key[key]['heading']}\n{rows_by_key[key]['content']}" for key, _ in ordered
        ]
        rerank_scores = reranker.score(query, texts)
        reranked_by = reranker.id
        # Stable on ties: fall back to the fused order (its index in `ordered`).
        ordered = [
            (key, score)
            for score, _, (key, _) in sorted(
                zip(rerank_scores, range(len(ordered)), ordered, strict=True),
                key=lambda t: (-t[0], t[1]),
            )
        ]

    hits = []
    for key, score in ordered[:top_k]:
        row = rows_by_key[key]
        hits.append(
            SearchHit(
                contract_id=row["contract_id"],
                clause_no=row["clause_no"],
                part=row["part"],
                heading=row["heading"],
                content=row["content"],
                artist_id=row["artist_id"],
                artist_name=row["artist_name"],
                kind=row["kind"],
                effective_from=row["effective_from"],
                effective_to=row["effective_to"],
                score=score,
                fts_rank=fts_rank_of.get(key),
                vec_rank=vec_rank_of.get(key),
            )
        )
    return SearchResult(
        hits=hits,
        mode="hybrid" if stored_model is not None else "fts-only",
        embedder_id=embedder_id,
        reranked_by=reranked_by,
    )
