"""Artist resolution shared by the agent tools.

Agents name artists the way users do — stage name, sometimes legal name, sometimes
misspelled. Resolution is exact-first (case-insensitive stage/legal match), then a
contains-search that either resolves uniquely or fails loudly *with candidates*, so a
"no such artist" outcome carries what the model needs to correct itself or abstain.
"""

from __future__ import annotations

from dataclasses import dataclass

import asyncpg


@dataclass(frozen=True)
class ArtistRef:
    id: int
    stage_name: str


async def resolve_artist(
    source: asyncpg.Pool | asyncpg.Connection,
    *,
    artist: str | None = None,
    artist_id: int | None = None,
) -> ArtistRef:
    """Resolve to exactly one artist or raise ``LookupError`` with guidance."""
    if artist_id is not None:
        row = await source.fetchrow(
            "SELECT id, stage_name FROM label.artists WHERE id = $1", artist_id
        )
        if row is None:
            raise LookupError(f"no artist with id {artist_id}")
        return ArtistRef(id=row["id"], stage_name=row["stage_name"])

    assert artist is not None
    name = artist.strip()
    rows = await source.fetch(
        "SELECT id, stage_name FROM label.artists "
        "WHERE lower(stage_name) = lower($1) OR lower(legal_name) = lower($1)",
        name,
    )
    if len(rows) == 1:
        return ArtistRef(id=rows[0]["id"], stage_name=rows[0]["stage_name"])
    if not rows:
        candidates = await source.fetch(
            "SELECT stage_name FROM label.artists "
            "WHERE stage_name ILIKE '%' || $1 || '%' OR legal_name ILIKE '%' || $1 || '%' "
            "ORDER BY stage_name LIMIT 5",
            name,
        )
        if len(candidates) == 1:
            return await resolve_artist(source, artist=candidates[0]["stage_name"])
        if candidates:
            names = ", ".join(c["stage_name"] for c in candidates)
            raise LookupError(f"no artist named {name!r} — did you mean: {names}?")
        raise LookupError(f"no artist named {name!r} on the roster")
    names = ", ".join(r["stage_name"] for r in rows)
    raise LookupError(f"artist name {name!r} is ambiguous between: {names}")
