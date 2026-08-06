"""``search_contracts`` + ``read_clause`` — the Counsel-facing retrieval tools (§4.3).

``search_contracts`` runs the full §4.4 pipeline (governing filter → hybrid → RRF →
rerank) and renders hits with *structural* citations (``FBR-C-00501 §3``) — contract
code + clause number, never vibes. Artist-scoped results open with the artist's full
governing-document inventory (every era base + effective amendments, with windows and
supersession marks): under D-003 a lapsed era still governs its recordings, so the
answer to a terms question may live in a document that never ranks into ``top_k`` —
the inventory makes that document visible instead of leaving coverage to ranking
(Phase 6 verification, finding 1). Snippets are query-aware for the same reason: a
fixed clause-head excerpt structurally hid every rate-card line past the second.
``read_clause`` fetches one clause verbatim for post-retrieval verification — flagging
base clauses an effective amendment replaced — and on a miss lists what exists
(abstention material, not a dead end).

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

import re
from datetime import date

from pydantic import BaseModel, ConfigDict, Field

from backline.core.runtime import Tool
from backline.rag.governing import SECTION_CLAUSE, GoverningDoc
from backline.rag.reranker import Reranker, get_reranker
from backline.rag.search import SearchResult, search_chunks
from backline.tools.artists import resolve_artist
from backline.tools.context import ToolContext

_CODE = {"base": "FBR-C", "amendment": "FBR-A"}
_SNIPPET_CHARS = 240
_TOKEN = re.compile(r"[a-z0-9]+")
_CLAUSE_SECTION = {clause: section for section, clause in SECTION_CLAUSE.items()}


def contract_code(kind: str, contract_id: int) -> str:
    return f"{_CODE[kind]}-{contract_id:05d}"


def query_snippet(content: str, query: str, limit: int = _SNIPPET_CHARS) -> str:
    """The ``limit``-char window of ``content`` densest in query-token matches.

    Tokens match by prefix ("sync" lights up "synchronization"), so the relevant
    region of a long clause surfaces instead of its boilerplate head. Deterministic:
    ties resolve to the earliest window; no matches falls back to the head. Ellipsis
    marks make any cut explicit.
    """
    text = " ".join(content.split())
    if len(text) <= limit:
        return text
    wanted = {t for t in _TOKEN.findall(query.lower()) if len(t) >= 3}
    spans = (
        [
            match.span()
            for match in _TOKEN.finditer(text.lower())
            if any(match.group(0).startswith(term) for term in wanted)
        ]
        if wanted
        else []
    )
    if not spans:
        return text[: limit - 1] + "…"
    best_i, best_j = 0, 1
    j = 1
    for i in range(len(spans)):
        j = max(j, i + 1)
        while j < len(spans) and spans[j][1] - spans[i][0] <= limit:
            j += 1
        if j - i > best_j - best_i:
            best_i, best_j = i, j
    span_start, span_end = spans[best_i][0], spans[best_j - 1][1]
    pad = max(0, (limit - (span_end - span_start)) // 2)
    start = max(0, min(span_start - pad, len(text) - limit))
    end = start + limit
    head = "…" if start > 0 else ""
    tail = "…" if end < len(text) else ""
    return head + text[start:end].strip() + tail


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


def _inventory_lines(governing: tuple[GoverningDoc, ...], as_of: date) -> list[str]:
    """The artist's full governing set — every era base and effective amendment.

    Ranking decides which clauses show as hits; this block guarantees no governing
    *document* is invisible. Multi-era artists are the point: each base governs the
    recordings originally released during its term (a lapsed term keeps governing its
    era's recordings — D-003), so several rate cards can govern concurrently.
    """
    bases = sum(1 for d in governing if d.kind == "base")
    amendments = len(governing) - bases
    lines = [
        f"Governing documents for this artist as of {as_of}: {bases} base agreement"
        + ("s" if bases != 1 else "")
        + (f" and {amendments} amendment" + ("s" if amendments != 1 else "") if amendments else "")
        + ". Each base governs recordings originally released during its term; an ended"
        " term still governs its era's recordings (post-term accounting). Check every"
        " era's terms before concluding a rate does not exist."
    ]
    for doc in governing:
        code = contract_code(doc.kind, doc.contract_id)
        window = f"effective {doc.effective_from}→" + (
            str(doc.effective_to) if doc.effective_to else ""
        )
        note = ""
        if doc.kind == "amendment" and doc.supersedes_contract_id is not None:
            target = contract_code("base", doc.supersedes_contract_id)
            note = f" — replaces {', '.join(doc.replaces) or 'sections'} of {target}"
        elif doc.excluded_clauses:
            note = f" — {', '.join(doc.excluded_clauses)} replaced by an effective amendment"
        lines.append(f"- {code} ({doc.kind}, {window}){note}")
    return lines


def _render_hits(
    result: SearchResult, as_of: date, include_history: bool, query: str, artist_scoped: bool
) -> str:
    if artist_scoped and not include_history and not result.governing:
        return (
            f"No documents govern this artist as of {as_of} — nothing is searchable at "
            f"that date. Check the as_of_date (the artist's first agreement may begin "
            f"later), or set include_history=true."
        )
    inventory = _inventory_lines(result.governing, as_of)
    if not result.hits:
        no_match = (
            f"No clauses matched (searched "
            f"{'all documents' if include_history else 'governing documents'} "
            f"as of {as_of}). Try different wording, drop the artist filter, or set "
            f"include_history=true for superseded terms."
        )
        return "\n".join([no_match, *inventory])
    stages = result.mode + (", reranked" if result.reranked_by else "")
    scope = "all documents incl. superseded" if include_history else "governing documents"
    lines = [f"{len(result.hits)} clauses from {scope} as of {as_of} ({stages}):", *inventory]
    for n, hit in enumerate(result.hits, start=1):
        code = contract_code(hit.kind, hit.contract_id)
        window = f"effective {hit.effective_from}" + (
            f"→{hit.effective_to}" if hit.effective_to else "→"
        )
        part = f" (part {hit.part})" if hit.part else ""
        lines.append(
            f"{n}. {code} {hit.clause_no}{part} — {hit.heading} "
            f"[{hit.artist_name}, {hit.kind}, {window}]"
        )
        lines.append(f"   <document>{query_snippet(hit.content, query)}</document>")
    lines.append(
        "Cite as `CODE §N` (e.g. "
        f"`{contract_code(result.hits[0].kind, result.hits[0].contract_id)} "
        f"{result.hits[0].clause_no}`). Snippets are windows — verify exact wording with "
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
        return _render_hits(
            result,
            as_of,
            params.include_history,
            params.query,
            artist_scoped=artist_id is not None,
        )

    return Tool(
        name="search_contracts",
        description=(
            "Search contract clauses (hybrid lexical+semantic over clause-level chunks). "
            "By default only documents *governing* as of as_of_date are searched — base "
            "contracts minus amendment-superseded sections plus effective amendments; set "
            "include_history=true for questions about past/superseded terms. Artist-scoped "
            "results begin with the artist's complete governing-document inventory: every "
            "era's base agreement governs its own recordings (an ended term still governs "
            "its era), so check each era's terms before concluding something is absent. "
            "Returns structural citations (contract code + clause number) with query-"
            "focused snippets."
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
        note = ""
        if first["kind"] == "base" and first["clause_no"] in _CLAUSE_SECTION:
            replacers = await ctx.pool.fetch(
                """
                SELECT a.amendment_id, c.effective_from
                FROM label.amendments a
                JOIN label.contracts c ON c.id = a.amendment_id
                WHERE a.supersedes_contract_id = $1
                  AND a.replaced_sections @> ARRAY[$2::text]
                ORDER BY c.effective_from, a.amendment_id
                """,
                params.contract_id,
                _CLAUSE_SECTION[first["clause_no"]],
            )
            if replacers:
                amended = ", ".join(
                    f"{contract_code('amendment', r['amendment_id'])} "
                    f"(effective {r['effective_from']})"
                    for r in replacers
                )
                note = (
                    f"\n\nNote: this clause has been replaced by {amended} — on/after "
                    f"that effective date the amendment governs, not this text."
                )
        return (
            f"{code} {first['clause_no']} — {first['heading']}\n"
            f"[{first['stage_name']}, {first['kind']}, {window}]\n\n"
            f'<document contract="{code}" clause="{first["clause_no"]}">\n'
            f"{body}\n</document>{note}"
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
