"""Keyless demo chat (D-024): scripted MockProvider turns through the real platform.

With no provider configured, the API still has to demo the whole product loop on a
cold clone — that is the Phase 6 DoD ("all four surfaces functional against seeded
data") and the Playwright smoke path, and invariant 8 keeps CI keyless. The move is
the eval-smoke precedent: **only the model is scripted**. The router still runs as a
traced run, the agent loop executes *real* tools against seeded Postgres (retrieval,
read-only SQL, anomaly scan, allocations, the gated ``submit_batch`` write), every
span lands in the tracer, and the Review Queue receives a real proposed batch.

Scripts are built per message, deterministically, from the seeded world:

- classification is keyword-based, and the canned prose is *computed from the same
  label-schema data the real agents read* (never ``truth``) — rates resolve through
  ``royaltycalc.resolve_terms``, analytics rows come from executing the exact SQL the
  script will hand the sql_query tool, allocations come from ``compute_ledger_slice``;
- every demo run/message is labeled ``demo: true`` so the UI can say what it is.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

import asyncpg

from backline.agents.router import RouteDecision
from backline.api.state import jload
from backline.providers.base import ToolCall, Usage
from backline.providers.mock import MockProvider, MockTurn
from backline.royaltycalc import TermsDoc, parse_terms_doc, resolve_terms
from backline.tools.ledger import compute_ledger_slice
from backline.tools.scan import SEVERITY_BY_KIND, ScanReport, run_scan

DEMO_PLANNER_MODEL = "mock-sonnet"
DEMO_ROUTER_MODEL = "mock-haiku"
DEMO_UTILITY_MODEL = "mock-haiku"

_PERIOD = re.compile(r"\b(20\d{2})-(0[1-9]|1[0-2])\b")
_DEMO_ALLOCATION_LIMIT = 8

_COUNSEL_HINTS = (
    "contract", "clause", "rate", "royalt", "term", "sync", "amendment",
    "advance", "recoup", "deal", "carve", "escalator", "guarantee", "territory clause",
)  # fmt: skip
_ANALYST_HINTS = (
    "top ", "revenue", "how many", "count", "most", "streams", "units",
    "territory", "store", "tracks", "catalog", "q1", "q2", "q3", "q4", "total",
)  # fmt: skip
_RECONCILER_HINTS = (
    "reconcile", "statement", "inbox", "drop", "ingest", "batch", "anomal", "flag",
)  # fmt: skip


def _usage_for(text: str, *, base_in: int = 900) -> Usage:
    """Plausible token usage for a scripted turn (costs stay realistic, not zero)."""
    return Usage(input_tokens=base_in, output_tokens=max(20, len(text) // 4))


@dataclass
class DemoPlan:
    """Everything one demo chat turn needs: the script and the models to run it on."""

    decision: RouteDecision
    turns: list[MockTurn] = field(default_factory=list)

    def provider(self) -> MockProvider:
        """Router turn first, then the agent turns — one consuming script."""
        route_call = ToolCall(
            id="route_demo",
            name="route",
            arguments=self.decision.model_dump(mode="json", exclude_none=True),
        )
        return MockProvider([MockTurn(tool_calls=[route_call]), *self.turns])


async def _match_artist(pool: asyncpg.Pool, message: str) -> tuple[int, str] | None:
    """Longest stage-name substring match (case-insensitive) in the message."""
    rows = await pool.fetch("SELECT id, stage_name FROM label.artists")
    lowered = message.lower()
    best: tuple[int, str] | None = None
    for row in rows:
        name = row["stage_name"]
        if name.lower() in lowered and (best is None or len(name) > len(best[1])):
            best = (row["id"], name)
    return best


def _classify(message: str, artist: tuple[int, str] | None) -> str:
    lowered = message.lower()

    def hits(hints: tuple[str, ...]) -> int:
        return sum(1 for h in hints if h in lowered)

    scores = {
        "reconciler": hits(_RECONCILER_HINTS),
        "counsel": hits(_COUNSEL_HINTS),
        "analyst": hits(_ANALYST_HINTS),
    }
    target = max(scores, key=lambda k: scores[k])
    if scores[target] == 0:
        return "counsel" if artist is not None else "clarify"
    return target


async def _latest_period(pool: asyncpg.Pool) -> str:
    period = await pool.fetchval("SELECT MAX(period) FROM label.statements")
    return str(period or "2026-06")


# ── counsel ──────────────────────────────────────────────────────────────────


async def _load_docs(pool: asyncpg.Pool, artist_id: int) -> list[TermsDoc]:
    rows = await pool.fetch(
        "SELECT t.terms FROM label.contracts c "
        "JOIN label.contract_terms t ON t.contract_id = c.id "
        "WHERE c.artist_id = $1 ORDER BY c.effective_from, c.id",
        artist_id,
    )
    return [parse_terms_doc(jload(row["terms"])) for row in rows]


def _governing(docs: list[TermsDoc], as_of: date) -> tuple[TermsDoc, list[TermsDoc]] | None:
    bases = [d for d in docs if d.kind == "base" and d.effective_from <= as_of]
    if not bases:
        return None
    base = bases[-1]
    amendments = [
        d
        for d in docs
        if d.kind == "amendment"
        and d.effective_from <= as_of
        and d.sections  # amendments tie to their base via replaced sections
    ]
    return base, amendments


async def _counsel_plan(
    pool: asyncpg.Pool, message: str, artist: tuple[int, str], decision: RouteDecision
) -> DemoPlan:
    artist_id, stage_name = artist
    as_of = date.today()
    docs = await _load_docs(pool, artist_id)
    search_call = ToolCall(
        id="demo_search",
        name="search_contracts",
        arguments={"query": message[:200], "artist": stage_name},
    )
    era = _governing(docs, as_of)
    if era is None:
        text = (
            f"ABSTAIN: no contract for {stage_name} is effective as of {as_of} in the "
            f"corpus — I won't guess terms that aren't in a governing document."
        )
        return DemoPlan(
            decision=decision,
            turns=[
                MockTurn(tool_calls=[search_call], usage=_usage_for("search")),
                MockTurn(text=text, usage=_usage_for(text)),
            ],
        )

    base, amendments = era
    # Amendments here amend this era's base; resolve through the one royalty engine.
    active = [a for a in amendments if a.effective_from >= base.effective_from]
    terms = resolve_terms(base, active, as_of=as_of)
    base_code = f"FBR-C-{base.contract_id:05d}"
    rates = {(e.revenue_type, e.territory): e.rate for e in terms.rate_card}

    def pct(value: Decimal) -> str:
        return f"{(value * 100).normalize():f}%"

    rate_lines = [
        f"- {rtype} ({terr if terr != 'WW' else 'worldwide'}): {pct(rate)}"
        for (rtype, terr), rate in sorted(rates.items())
    ]
    amended_note = ""
    if active:
        codes = ", ".join(f"FBR-A-{a.contract_id:05d}" for a in active)
        amended_note = f" as amended by {codes}"
    text = (
        f"As of {as_of}, {stage_name}'s governing terms come from {base_code} "
        f"(base, effective {base.effective_from}){amended_note}.\n\n"
        f"Resolved rate card (via royaltycalc.resolve_terms):\n"
        + "\n".join(rate_lines)
        + (
            f"\n\nExcluded territories: {', '.join(sorted(terms.excluded_territories))}."
            if terms.excluded_territories
            else ""
        )
        + f"\n\nSource: {base_code} §3 (Royalties)."
    )
    return DemoPlan(
        decision=decision,
        turns=[
            MockTurn(tool_calls=[search_call], usage=_usage_for("search")),
            MockTurn(
                tool_calls=[
                    ToolCall(
                        id="demo_read",
                        name="read_clause",
                        arguments={"contract_id": base.contract_id, "clause_no": "§3"},
                    )
                ],
                usage=_usage_for("read"),
            ),
            MockTurn(text=text, usage=_usage_for(text)),
        ],
    )


# ── analyst ──────────────────────────────────────────────────────────────────

_ANALYST_SQL = """\
SELECT t.title, a.stage_name,
       SUM(l.units) AS units,
       ROUND(SUM(l.gross_amount * fx.usd_rate), 2) AS gross_usd
FROM label.statement_lines l
JOIN label.tracks t ON t.isrc = l.isrc
JOIN label.artists a ON a.id = t.primary_artist_id
JOIN label.fx_rates fx ON fx.period = l.period AND fx.currency = l.currency
WHERE l.period = '{period}'
GROUP BY t.title, a.stage_name
ORDER BY gross_usd DESC
LIMIT 5"""


async def _analyst_plan(pool: asyncpg.Pool, message: str, decision: RouteDecision) -> DemoPlan:
    match = _PERIOD.search(message)
    period = match.group(0) if match else await _latest_period(pool)
    sql = _ANALYST_SQL.format(period=period)
    rows = await pool.fetch(sql)
    if rows:
        listing = "\n".join(
            f"{i}. “{r['title']}” — {r['stage_name']}: "
            f"{r['units']:,} units, ${r['gross_usd']} gross (USD-normalized)"
            for i, r in enumerate(rows, start=1)
        )
        text = (
            f"Top tracks by gross revenue for {period}, from one read-only SQL query "
            f"over label.statement_lines (FX-normalized to USD via label.fx_rates):\n\n"
            f"{listing}\n\nThe executed query and its result table are in this run's "
            f"trace (tool: sql_query)."
        )
    else:
        text = (
            f"No statement lines are ingested for {period} — the query ran and "
            f"returned zero rows. Try an ingested period or reconcile the drop first."
        )
    return DemoPlan(
        decision=decision,
        turns=[
            MockTurn(
                tool_calls=[ToolCall(id="demo_sql", name="sql_query", arguments={"query": sql})],
                usage=_usage_for(sql),
            ),
            MockTurn(text=text, usage=_usage_for(text)),
        ],
    )


# ── reconciler ───────────────────────────────────────────────────────────────


def _flags_from_scan(report: ScanReport) -> list[dict[str, object]]:
    return [
        {
            "kind": c.kind,
            "severity": SEVERITY_BY_KIND.get(c.kind, "warning"),
            "payload": {
                "source": c.source,
                "line_id": c.line_id,
                "statement_id": c.statement_id,
                "detail": c.detail,
            },
        }
        for c in report.candidates
    ]


async def _top_artists_by_gross(
    pool: asyncpg.Pool, period: str, limit: int
) -> tuple[list[int], int]:
    """(top-N artist ids, total candidates) by period gross across label+staged lines."""
    rows = await pool.fetch(
        """
        WITH lines AS (
            SELECT isrc, gross_amount, currency, period FROM label.statement_lines
            WHERE period = $1
            UNION ALL
            SELECT isrc, gross_amount, currency, period FROM staging.ingested_lines
            WHERE period = $1
        )
        SELECT t.primary_artist_id AS artist_id,
               SUM(l.gross_amount * fx.usd_rate) AS gross_usd
        FROM lines l
        JOIN label.tracks t ON l.isrc <> '' AND t.isrc = l.isrc
        JOIN label.fx_rates fx ON fx.period = l.period AND fx.currency = l.currency
        GROUP BY t.primary_artist_id
        ORDER BY gross_usd DESC
        """,
        period,
    )
    return [r["artist_id"] for r in rows[:limit]], len(rows)


async def _reconciler_plan(pool: asyncpg.Pool, message: str, decision: RouteDecision) -> DemoPlan:
    match = _PERIOD.search(message)
    period = match.group(0) if match else await _latest_period(pool)
    turns: list[MockTurn] = []

    # Fresh-drop story: one received statement with nothing staged yet → ingest+match.
    pending = await pool.fetchrow(
        "SELECT s.id, s.raw_path FROM label.statements s "
        "WHERE s.period = $1 AND s.status = 'received' "
        "AND NOT EXISTS (SELECT 1 FROM staging.ingested_lines i WHERE i.statement_id = s.id) "
        "ORDER BY s.id LIMIT 1",
        period,
    )
    if pending is not None:
        turns.append(
            MockTurn(
                tool_calls=[
                    ToolCall(
                        id="demo_ingest",
                        name="ingest_statement",
                        arguments={"path": pending["raw_path"]},
                    )
                ],
                usage=_usage_for("ingest"),
            )
        )
        turns.append(
            MockTurn(
                tool_calls=[
                    ToolCall(
                        id="demo_match",
                        name="match_lines",
                        arguments={"statement_id": pending["id"]},
                    )
                ],
                usage=_usage_for("match"),
            )
        )

    scan = await run_scan(pool, period=period)
    flags = _flags_from_scan(scan)
    exclude_label, exclude_staged = scan.suggested_exclusions()
    top_ids, n_candidates = await _top_artists_by_gross(pool, period, _DEMO_ALLOCATION_LIMIT)

    allocations: list[dict[str, object]] = []
    total = Decimal("0")
    for artist_id in top_ids:
        s = await compute_ledger_slice(
            pool,
            artist_id=artist_id,
            period=period,
            exclude_line_ids=tuple(exclude_label),
            exclude_staged_line_ids=tuple(exclude_staged),
            include_staged=True,
        )
        if s.net_payable <= 0:
            continue
        total += s.net_payable
        allocations.append(
            {
                "artist_id": artist_id,
                "net_payable": str(s.net_payable),
                "line_detail": {
                    "gross": str(s.gross),
                    "recouped": str(s.recouped),
                    "balance_after": str(s.balance_after),
                },
            }
        )

    note = (
        f"Demo-scripted reconciliation (keyless mode, D-024): allocations cover the "
        f"top {len(allocations)} artists by {period} gross out of {n_candidates} with "
        f"reported revenue; {len(exclude_label) + len(exclude_staged)} scan-suggested "
        f"line exclusion(s) applied. Amounts computed by royaltycalc."
    )
    turns.append(
        MockTurn(
            tool_calls=[
                ToolCall(id="demo_scan", name="scan_anomalies", arguments={"period": period})
            ],
            usage=_usage_for("scan"),
        )
    )
    turns.append(
        MockTurn(
            tool_calls=[
                ToolCall(
                    id="demo_submit",
                    name="submit_batch",
                    arguments={
                        "period": period,
                        "allocations": allocations,
                        "flags": flags,
                        "note": note,
                    },
                )
            ],
            usage=_usage_for(note),
        )
    )
    kinds = sorted({str(f["kind"]) for f in flags})
    flag_summary = (
        f"{len(flags)} candidate flag(s): {', '.join(kinds)}" if flags else "no flags raised"
    )
    ingest_note = (
        f"Ingested {pending['raw_path']} and matched its lines to the catalog, then "
        if pending is not None
        else ""
    )
    # No ``BATCH: <id>`` wrap-up line here: that contract exists for eval scoring,
    # and a script cannot know the id ``submit_batch`` will get. Chat resolves the
    # real id from staging by run id and renders it as the batch link (chat.py).
    text = (
        f"{ingest_note}scanned {period} ({scan.n_lines:,} lines across "
        f"{scan.n_statements} statements) and submitted the proposed batch for human "
        f"review — nothing posts until a reviewer approves it in the Review Queue.\n\n"
        f"{note}\n\n"
        f"FLAGS: {flag_summary}"
    )
    turns.append(MockTurn(text=text, usage=_usage_for(text)))
    return DemoPlan(decision=decision, turns=turns)


# ── entry point ──────────────────────────────────────────────────────────────


async def build_demo_plan(
    pool: asyncpg.Pool, message: str, *, pinned_agent: str | None = None
) -> DemoPlan:
    """Deterministic plan for one chat turn: route decision + scripted agent turns."""
    artist = await _match_artist(pool, message)
    target = pinned_agent or _classify(message, artist)
    artists = [artist[1]] if artist else []

    if target == "clarify":
        decision = RouteDecision(
            target="clarify",
            confidence=0.34,
            reason="demo script: no counsel/analyst/reconciler signal in the message",
            clarifying_question=(
                "I can look up contract terms (Counsel), run catalog/revenue analytics "
                "(Analyst), or process statement drops (Reconciler) — which do you "
                "need, and for which artist or period?"
            ),
            artists=artists,
        )
        return DemoPlan(decision=decision)

    decision = RouteDecision(
        target=target,  # type: ignore[arg-type]  # validated by RouteDecision
        confidence=0.95 if pinned_agent else 0.9,
        reason=(
            "demo script: user pinned the agent"
            if pinned_agent
            else "demo script: keyword classification"
        ),
        artists=artists,
    )
    if target == "counsel":
        if artist is None:
            # No artist to ground the lookup: an honest scripted abstention.
            text = (
                "ABSTAIN: I can't tell which artist's contracts to search — name the "
                "artist (e.g. “what is Nova Reyes' sync rate?”) and I'll cite "
                "the governing clause."
            )
            return DemoPlan(decision=decision, turns=[MockTurn(text=text)])
        return await _counsel_plan(pool, message, artist, decision)
    if target == "analyst":
        return await _analyst_plan(pool, message, decision)
    return await _reconciler_plan(pool, message, decision)
