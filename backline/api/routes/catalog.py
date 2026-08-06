"""Catalog browse endpoints: artists, releases, tracks, and clause resolution.

Read-only views over the ``label`` schema (never ``truth``). ``/catalog/clauses``
resolves the structural citations agents emit (``FBR-C-00501 §3``) to the exact
clause text — the Chat surface's citation drawer calls it.
"""

from __future__ import annotations

import re
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException

from backline.api.schemas import (
    ArtistDetailOut,
    ArtistListOut,
    ArtistOut,
    ClauseOut,
    ContractOut,
    ReleaseListOut,
    ReleaseOut,
    TrackListOut,
    TrackOut,
)
from backline.api.state import AppState, get_state

router = APIRouter(prefix="/catalog", tags=["catalog"])

State = Annotated[AppState, Depends(get_state)]

_CODE = re.compile(r"^FBR-(?P<kind>[CA])-(?P<id>\d{5})$")


def _contract_code(contract_id: int, kind: str) -> str:
    prefix = "FBR-C" if kind == "base" else "FBR-A"
    return f"{prefix}-{contract_id:05d}"


@router.get("/artists", response_model=ArtistListOut)
async def list_artists(
    state: State, q: str = "", limit: int = 50, offset: int = 0
) -> ArtistListOut:
    pattern = f"%{q}%"
    total = await state.pool.fetchval(
        "SELECT count(*) FROM label.artists WHERE stage_name ILIKE $1 OR legal_name ILIKE $1",
        pattern,
    )
    rows = await state.pool.fetch(
        """
        SELECT a.id, a.stage_name, a.legal_name, a.joined_at,
               (SELECT count(*) FROM label.tracks t WHERE t.primary_artist_id = a.id)
                   AS n_tracks,
               (SELECT count(DISTINCT rt.release_id) FROM label.tracks t
                JOIN label.release_tracks rt ON rt.track_id = t.id
                WHERE t.primary_artist_id = a.id) AS n_releases,
               (SELECT count(*) FROM label.contracts c WHERE c.artist_id = a.id)
                   AS n_contracts
        FROM label.artists a
        WHERE a.stage_name ILIKE $1 OR a.legal_name ILIKE $1
        ORDER BY a.stage_name
        LIMIT $2 OFFSET $3
        """,
        pattern,
        min(limit, 200),
        max(offset, 0),
    )
    return ArtistListOut(artists=[ArtistOut(**dict(r)) for r in rows], total=total)


@router.get("/artists/{artist_id}", response_model=ArtistDetailOut)
async def get_artist(artist_id: int, state: State) -> ArtistDetailOut:
    row = await state.pool.fetchrow(
        """
        SELECT a.id, a.stage_name, a.legal_name, a.joined_at,
               (SELECT count(*) FROM label.tracks t WHERE t.primary_artist_id = a.id)
                   AS n_tracks,
               (SELECT count(DISTINCT rt.release_id) FROM label.tracks t
                JOIN label.release_tracks rt ON rt.track_id = t.id
                WHERE t.primary_artist_id = a.id) AS n_releases,
               (SELECT count(*) FROM label.contracts c WHERE c.artist_id = a.id)
                   AS n_contracts
        FROM label.artists a WHERE a.id = $1
        """,
        artist_id,
    )
    if row is None:
        raise HTTPException(status_code=404, detail=f"no artist {artist_id}")
    releases = await state.pool.fetch(
        """
        SELECT r.id, r.upc, r.title, r.imprint, r.release_date,
               count(rt.track_id) AS n_tracks
        FROM label.releases r
        JOIN label.release_tracks rt ON rt.release_id = r.id
        JOIN label.tracks t ON t.id = rt.track_id
        WHERE t.primary_artist_id = $1
        GROUP BY r.id ORDER BY r.release_date DESC
        """,
        artist_id,
    )
    contracts = await state.pool.fetch(
        "SELECT id, kind, effective_from, effective_to, doc_path FROM label.contracts "
        "WHERE artist_id = $1 ORDER BY effective_from, id",
        artist_id,
    )
    return ArtistDetailOut(
        artist=ArtistOut(**dict(row)),
        releases=[ReleaseOut(**dict(r)) for r in releases],
        contracts=[
            ContractOut(**dict(c), code=_contract_code(c["id"], c["kind"])) for c in contracts
        ],
    )


@router.get("/releases", response_model=ReleaseListOut)
async def list_releases(
    state: State, q: str = "", imprint: str | None = None, limit: int = 50, offset: int = 0
) -> ReleaseListOut:
    conditions = ["(r.title ILIKE $1 OR r.upc LIKE $1)"]
    args: list[Any] = [f"%{q}%"]
    if imprint is not None:
        args.append(imprint)
        conditions.append(f"r.imprint = ${len(args)}")
    where = " AND ".join(conditions)
    total = await state.pool.fetchval(f"SELECT count(*) FROM label.releases r WHERE {where}", *args)
    args.extend((min(limit, 200), max(offset, 0)))
    rows = await state.pool.fetch(
        f"""
        SELECT r.id, r.upc, r.title, r.imprint, r.release_date,
               (SELECT count(*) FROM label.release_tracks rt WHERE rt.release_id = r.id)
                   AS n_tracks
        FROM label.releases r WHERE {where}
        ORDER BY r.release_date DESC, r.id
        LIMIT ${len(args) - 1} OFFSET ${len(args)}
        """,
        *args,
    )
    return ReleaseListOut(releases=[ReleaseOut(**dict(r)) for r in rows], total=total)


@router.get("/tracks", response_model=TrackListOut)
async def list_tracks(
    state: State,
    q: str = "",
    artist_id: int | None = None,
    limit: int = 50,
    offset: int = 0,
) -> TrackListOut:
    conditions = ["(t.title ILIKE $1 OR t.isrc LIKE $1)"]
    args: list[Any] = [f"%{q}%"]
    if artist_id is not None:
        args.append(artist_id)
        conditions.append(f"t.primary_artist_id = ${len(args)}")
    where = " AND ".join(conditions)
    total = await state.pool.fetchval(f"SELECT count(*) FROM label.tracks t WHERE {where}", *args)
    args.extend((min(limit, 200), max(offset, 0)))
    rows = await state.pool.fetch(
        f"""
        SELECT t.id, t.isrc, t.title, t.duration_s, t.primary_artist_id, a.stage_name
        FROM label.tracks t
        JOIN label.artists a ON a.id = t.primary_artist_id
        WHERE {where}
        ORDER BY t.title, t.id
        LIMIT ${len(args) - 1} OFFSET ${len(args)}
        """,
        *args,
    )
    return TrackListOut(tracks=[TrackOut(**dict(r)) for r in rows], total=total)


@router.get("/clauses/{code}/{clause_no}", response_model=ClauseOut)
async def get_clause(code: str, clause_no: str, state: State) -> ClauseOut:
    """Resolve a structural citation to its clause text (chunks are the source —
    the same text retrieval served the agent)."""
    match = _CODE.match(code.strip().upper())
    if match is None:
        raise HTTPException(
            status_code=422, detail=f"bad contract code {code!r} (want FBR-C-NNNNN / FBR-A-NNNNN)"
        )
    contract_id = int(match.group("id"))
    clause = clause_no.strip().upper()
    if not clause.startswith("§"):
        clause = f"§{clause}"
    rows = await state.pool.fetch(
        """
        SELECT ch.contract_id, ch.clause_no, ch.heading, ch.content, ch.artist_id,
               ch.kind, ch.effective_from, ch.effective_to, a.stage_name
        FROM rag.contract_chunks ch
        LEFT JOIN label.artists a ON a.id = ch.artist_id
        WHERE ch.contract_id = $1 AND ch.clause_no = $2
        ORDER BY ch.part
        """,
        contract_id,
        clause,
    )
    if not rows:
        raise HTTPException(
            status_code=404,
            detail=f"no clause {clause} for {code} — run `make embed` to build the "
            f"chunk corpus, or check the citation",
        )
    first = rows[0]
    return ClauseOut(
        code=code.strip().upper(),
        contract_id=first["contract_id"],
        clause_no=first["clause_no"],
        heading=first["heading"],
        text="\n\n".join(r["content"] for r in rows),
        artist_id=first["artist_id"],
        stage_name=first["stage_name"],
        kind=first["kind"],
        effective_from=first["effective_from"],
        effective_to=first["effective_to"],
    )
