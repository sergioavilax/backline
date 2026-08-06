"""``save_note`` / ``recall_notes`` — durable entity-keyed memory (§4.3, §4.5 scope 3).

Notes are observations worth keeping across sessions ("Nova Reyes' JP carve-out trips
people up"), keyed to a structured ``entity_ref`` (``kind:key`` — e.g. ``artist:130``,
``contract:670``, ``feed:vantage``, ``period:2026-03``). Writes stamp the proposing run
(``created_by``) via the ambient run context. Auto-recall into agent context joins in
Phase 4 with the router's entity detection; the tools themselves are the durable API.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator

from backline.core.runcontext import current_run_id
from backline.core.runtime import Tool
from backline.tools.context import ToolContext

_RECALL_LIMIT = 20
_REF_PATTERN = r"^[a-z][a-z_]*:[A-Za-z0-9][A-Za-z0-9 _.-]*$"


class SaveNoteParams(BaseModel):
    model_config = ConfigDict(frozen=True)

    entity_ref: str = Field(
        pattern=_REF_PATTERN,
        description="structured key `kind:key`, e.g. artist:130, contract:670, feed:vantage",
    )
    text: str = Field(description="the observation to keep")

    @field_validator("text")
    @classmethod
    def _non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("note text must not be blank")
        return value.strip()


class RecallNotesParams(BaseModel):
    model_config = ConfigDict(frozen=True)

    entity_ref: str = Field(pattern=_REF_PATTERN, description="structured key `kind:key`")


def build_save_note_tool(ctx: ToolContext) -> Tool[SaveNoteParams]:
    async def handler(params: SaveNoteParams) -> str:
        note_id = await ctx.pool.fetchval(
            "INSERT INTO app.notes (entity_ref, body, created_by) VALUES ($1, $2, $3) RETURNING id",
            params.entity_ref,
            params.text,
            current_run_id.get(),
        )
        return f"Saved note {note_id} on {params.entity_ref}."

    return Tool(
        name="save_note",
        description=(
            "Save a durable observation keyed to an entity (`kind:key`, e.g. artist:130, "
            "contract:670, feed:vantage, period:2026-03) for future sessions. Use it for "
            "gotchas, resolved ambiguities, and recurring issues — not for scratch work."
        ),
        params=SaveNoteParams,
        handler=handler,
    )


def build_recall_notes_tool(ctx: ToolContext) -> Tool[RecallNotesParams]:
    async def handler(params: RecallNotesParams) -> str:
        rows = await ctx.pool.fetch(
            "SELECT body, created_at FROM app.notes WHERE entity_ref = $1 "
            "ORDER BY id DESC LIMIT $2",
            params.entity_ref,
            _RECALL_LIMIT,
        )
        if not rows:
            return f"No notes on {params.entity_ref}."
        lines = [f"{len(rows)} note(s) on {params.entity_ref}, newest first:"]
        lines.extend(f"- [{row['created_at']:%Y-%m-%d %H:%M}] {row['body']}" for row in rows)
        return "\n".join(lines)

    return Tool(
        name="recall_notes",
        description=(
            "Recall saved notes for an entity (`kind:key`). Check before deep-diving an "
            "artist, contract, or feed — earlier sessions may have left warnings."
        ),
        params=RecallNotesParams,
        handler=handler,
    )
