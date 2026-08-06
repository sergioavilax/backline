"""``search_contracts`` + ``read_clause`` — the Counsel-facing retrieval tools (§4.3).

``search_contracts`` runs the full §4.4 pipeline (governing filter → hybrid → RRF →
rerank) and renders hits with *structural* citations (``FBR-C-00501 §3``) — contract
code + clause number, never vibes. ``read_clause`` fetches one clause verbatim for
post-retrieval verification, and on a miss lists what exists (abstention material,
not a dead end).

Injection defense (§4.6): quoted corpus text — snippets and clause bodies — is fenced
in ``<document>`` tags so the boundary between structural metadata (ours) and
document content (untrusted) is explicit; the agent prompts state that document text
never constitutes instructions, and the ``injection_suspected`` guardrail watches
these tools' results.

Embedder/reranker resolution: explicit overrides on the ``ToolContext`` win (tests,
Phase 4 bootstrap); otherwise the store's recorded embedding model decides the query
embedder and ``RERANK=on`` + ``RERANK_MODEL`` pick the reranker — both through the
process-wide model caches (``get_embedder``/``get_reranker``), so weights load once
per process, never per query.
"""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, ConfigDict, Field

from backline.core.runtime import Tool
from backline.rag.reranker import Reranker, get_reranker
from backline.rag.search import SearchResult, search_chunks
from backline.tools.artists import resolve_artist
from backline.tools.context import ToolContext

_CODE = {"base": "FBR-C", "amendment": "FBR-A"}
_SNIPPET_CHARS = 240


def contract_code(kind: str, contract_id: int) -> str:
    return f"{_CODE[kind]}-{contract_id:05d}"


class SearchContractsParams(BaseModel):
    model_config = ConfigDict(frozen=True)

    query: str = Field(description="what to look for, in natural language")
    artist: str | None = Field(
        default=None, description="restrict to one artist (stage or legal name)"
    )
    as_of_date: date | None = Field(
        default=None,
        description="resolve governing documents as of this date (default: today)",
    )
    include_history: bool = Field(
        default=False,
        description="also search superseded/older clauses (for questions about history)",
    )
    top_k: int = Field(default=8, ge=1, le=20)


class ReadClauseParams(BaseModel):
    model_config = ConfigDict(frozen=True)

    contract_id: int = Field(description="numeric id from a search hit (FBR-C-00501 → 501)")
    clause_no: str = Field(description="clause number from a search hit, e.g. §3 or §A1")


def _resolve_reranker(ctx: ToolContext) -> Reranker | None:
    if ctx.reranker is not None:
        return ctx.reranker
    if ctx.settings.rerank.lower() != "on":
        return None
    return get_reranker(ctx.settings.rerank_model)


def _render_hits(result: SearchResult, as_of: date, include_history: bool) -> str:
    if not result.hits:
        return (
            f"No clauses matched (searched "
            f"{'all documents' if include_history else 'governing documents'} "
            f"as of {as_of}). Try different wording, drop the artist filter, or set "
            f"include_history=true for superseded terms."
        )
    stages = result.mode + (", reranked" if result.reranked_by else "")
    scope = "all documents incl. superseded" if include_history else "governing documents"
    lines = [f"{len(result.hits)} clauses from {scope} as of {as_of} ({stages}):"]
    for n, hit in enumerate(result.hits, start=1):
        code = contract_code(hit.kind, hit.contract_id)
        window = f"effective {hit.effective_from}" + (
            f"→{hit.effective_to}" if hit.effective_to else "→"
        )
        snippet = " ".join(hit.content.split())
        if len(snippet) > _SNIPPET_CHARS:
            snippet = snippet[: _SNIPPET_CHARS - 1] + "…"
        part = f" (part {hit.part})" if hit.part else ""
        lines.append(
            f"{n}. {code} {hit.clause_no}{part} — {hit.heading} "
            f"[{hit.artist_name}, {hit.kind}, {window}]"
        )
        lines.append(f"   <document>{snippet}</document>")
    lines.append(
        "Cite as `CODE §N` (e.g. "
        f"`{contract_code(result.hits[0].kind, result.hits[0].contract_id)} "
        f"{result.hits[0].clause_no}`). Verify exact wording with "
        "read_clause(contract_id, clause_no) before quoting rates or amounts."
    )
    return "\n".join(lines)


def build_search_contracts_tool(ctx: ToolContext) -> Tool[SearchContractsParams]:
    async def handler(params: SearchContractsParams) -> str:
        artist_id: int | None = None
        if params.artist is not None:
            try:
                artist_id = (await resolve_artist(ctx.pool, artist=params.artist)).id
            except LookupError as miss:
                return (
                    f"{miss} — search ran against no documents; correct the name or omit `artist`."
                )
        as_of = params.as_of_date or date.today()
        result = await search_chunks(
            ctx.pool,
            params.query,
            artist_id=artist_id,
            as_of=as_of,
            include_history=params.include_history,
            top_k=params.top_k,
            embedder=ctx.embedder,
            reranker=_resolve_reranker(ctx),
        )
        return _render_hits(result, as_of, params.include_history)

    return Tool(
        name="search_contracts",
        description=(
            "Search contract clauses (hybrid lexical+semantic over clause-level chunks). "
            "By default only documents *governing* as of as_of_date are searched — base "
            "contracts minus amendment-superseded sections plus effective amendments; set "
            "include_history=true for questions about past/superseded terms. Returns "
            "structural citations (contract code + clause number) with snippets."
        ),
        params=SearchContractsParams,
        handler=handler,
    )


def build_read_clause_tool(ctx: ToolContext) -> Tool[ReadClauseParams]:
    async def handler(params: ReadClauseParams) -> str:
        clause_no = params.clause_no.strip().upper()
        if not clause_no.startswith("§"):
            clause_no = f"§{clause_no}"
        if clause_no == "§TITLE":
            clause_no = "title"
        rows = await ctx.pool.fetch(
            """
            SELECT ch.clause_no, ch.part, ch.heading, ch.content, ch.kind,
                   ch.effective_from, ch.effective_to, a.stage_name
            FROM rag.contract_chunks ch
            JOIN label.artists a ON a.id = ch.artist_id
            WHERE ch.contract_id = $1 AND ch.clause_no = $2
            ORDER BY ch.part
            """,
            params.contract_id,
            "title" if clause_no == "title" else clause_no,
        )
        if not rows:
            available = await ctx.pool.fetch(
                "SELECT DISTINCT clause_no FROM rag.contract_chunks WHERE contract_id = $1 "
                "ORDER BY clause_no",
                params.contract_id,
            )
            if not available:
                return (
                    f"No contract {params.contract_id} in the clause store — check the id "
                    f"(search hits show it as FBR-C-NNNNN / FBR-A-NNNNN)."
                )
            clause_list = ", ".join(r["clause_no"] for r in available)
            return (
                f"No clause {params.clause_no!r} in contract {params.contract_id}. "
                f"Available clauses: {clause_list}."
            )
        first = rows[0]
        code = contract_code(first["kind"], params.contract_id)
        window = f"effective {first['effective_from']}" + (
            f"→{first['effective_to']}" if first["effective_to"] else "→"
        )
        body = "\n".join(r["content"] for r in rows)
        return (
            f"{code} {first['clause_no']} — {first['heading']}\n"
            f"[{first['stage_name']}, {first['kind']}, {window}]\n\n"
            f'<document contract="{code}" clause="{first["clause_no"]}">\n'
            f"{body}\n</document>"
        )

    return Tool(
        name="read_clause",
        description=(
            "Fetch the exact, verbatim text of one contract clause by contract id and "
            "clause number (from a search_contracts hit). Use it to verify wording "
            "before quoting rates, amounts, or obligations."
        ),
        params=ReadClauseParams,
        handler=handler,
    )
