"""Entity-keyed note auto-recall (§4.5 scope 3, the Phase 4 tail).

When the router detects artist entities in a message, their durable notes
(``app.notes``, written via ``save_note``) are folded into the agent's context
before the run starts — the "Nova Reyes' JP carve-out trips people up" warning
arrives without the agent having to think to ask. The block rides *inside the user
turn*, fenced, so what the model saw is exactly what the trace shows.

Resolution is exact-first via the shared artist resolver; names that don't resolve
uniquely are skipped silently — auto-recall is a convenience, never a failure path.
"""

from __future__ import annotations

import asyncpg

from backline.tools.artists import resolve_artist

_NOTES_PER_ENTITY = 5


async def recall_block(source: asyncpg.Pool | asyncpg.Connection, names: list[str]) -> str:
    """Render the ``<recalled_notes>`` block for these artist names ('' when none)."""
    sections: list[str] = []
    seen: set[int] = set()
    for name in names:
        try:
            artist = await resolve_artist(source, artist=name)
        except LookupError:
            continue
        if artist.id in seen:
            continue
        seen.add(artist.id)
        rows = await source.fetch(
            "SELECT body, created_at FROM app.notes WHERE entity_ref = $1 "
            "ORDER BY id DESC LIMIT $2",
            f"artist:{artist.id}",
            _NOTES_PER_ENTITY,
        )
        if not rows:
            continue
        sections.append(f"notes on artist:{artist.id} ({artist.stage_name}), newest first:")
        sections.extend(f"- [{row['created_at']:%Y-%m-%d}] {row['body']}" for row in rows)
    if not sections:
        return ""
    body = "\n".join(sections)
    return f"<recalled_notes>\n{body}\n</recalled_notes>"


def compose_user_message(message: str, recalled: str) -> str:
    """The user turn the routed agent receives: recalled context first, then the ask."""
    if not recalled:
        return message
    return f"{recalled}\n\n{message}"
