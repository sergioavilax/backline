"""``scan_anomalies`` — the Reconciler's flag heuristics (Phase 4, §3.4 kinds).

Deterministic tolerance rules per anomaly kind, run over a period's reported lines
(label lines for ingested statements + staged lines for received ones — the same
sourcing rule as ``match_lines``, D-010). The agent reviews the candidates and owns
the final flag list; the rules own the measurements. Precision is designed in: the
two seeded borderline cases (a dashboard gap inside tolerance, a legit first-territory
line at ~1.6x median) fall *below* these thresholds, and within-tolerance
measurements are reported as prose, never as flags.

Rules (kind → rule):

- ``duplicate_line``       same ``line_hash`` more than once within a statement; the
                           lowest id is kept, the rest are flagged.
- ``unknown_isrc``         non-blank ISRC matching no catalog track.
- ``currency_mismatch``    line currency differs from the feed's reporting currency
                           (GBP allowed where the feed's dialect says so, e.g.
                           northstar GB) — feed reference from ``datagen/world.yaml``,
                           the same label config the calculator reads (D-012).
- ``negative_units``       units < 0 or negative gross.
- ``period_bleed``         line dated outside its statement's period.
- ``sudden_territory_spike`` streaming line in a territory with zero prior history
                           for (isrc, store), at ≥ ``SPIKE_FACTOR`` x the track's
                           median historical per-line units on that store.
- ``dashboard_gap``        statement streaming totals per (isrc, store) vs
                           ``label.dashboard_streams`` diverging beyond the
                           configured tolerance (5%); statement money stays
                           authoritative — flag, never exclude.

Label and staged line ids are separate sequences; every candidate carries its
source, and suggested exclusions are emitted per source (``exclude_line_ids`` vs
``exclude_staged_line_ids``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache

import asyncpg
from pydantic import BaseModel, ConfigDict, Field

from backline.core.runtime import Tool
from backline.tools.context import ToolContext
from datagen.config import load_world_config

SPIKE_FACTOR = 4.0  # x median historical per-line units; the 1.6x borderline stays under
_SHOWN_PER_KIND = 12

# error: money-moving corruption (exclude + fix); warning: review signal.
SEVERITY_BY_KIND = {
    "duplicate_line": "error",
    "unknown_isrc": "error",
    "currency_mismatch": "error",
    "negative_units": "error",
    "period_bleed": "warning",
    "sudden_territory_spike": "warning",
    "dashboard_gap": "warning",
}

# Kinds whose lines are corrupt reporting and should be excluded from allocations.
# dashboard_gap is dashboard-side (D-005): statement money stays authoritative.
EXCLUDABLE_KINDS = frozenset(SEVERITY_BY_KIND) - {"dashboard_gap"}


@dataclass(frozen=True)
class ScanCandidate:
    kind: str
    source: str  # "label" | "staged"
    line_id: int
    statement_id: int
    detail: str


@dataclass
class ScanReport:
    period: str
    statement_id: int | None
    n_statements: int
    n_lines: int
    candidates: list[ScanCandidate] = field(default_factory=list)
    within_tolerance: list[str] = field(default_factory=list)  # measured, NOT flagged
    notes: list[str] = field(default_factory=list)

    def by_kind(self) -> dict[str, list[ScanCandidate]]:
        grouped: dict[str, list[ScanCandidate]] = {}
        for candidate in self.candidates:
            grouped.setdefault(candidate.kind, []).append(candidate)
        return grouped

    def suggested_exclusions(self) -> tuple[list[int], list[int]]:
        label = sorted(
            {
                c.line_id
                for c in self.candidates
                if c.kind in EXCLUDABLE_KINDS and c.source == "label"
            }
        )
        staged = sorted(
            {
                c.line_id
                for c in self.candidates
                if c.kind in EXCLUDABLE_KINDS and c.source == "staged"
            }
        )
        return label, staged


@lru_cache(maxsize=1)
def _feed_reference() -> tuple[dict[str, tuple[str, tuple[str, ...]]], tuple[str, ...]]:
    """(dialect → (currency, gbp_territories), streaming store names) from world.yaml."""
    config = load_world_config()
    by_dialect = {
        feed.dialect: (feed.currency, feed.gbp_territories) for feed in config.feeds.values()
    }
    streaming = tuple(s.name for s in config.stores if s.revenue_type == "streaming")
    return by_dialect, streaming


def _dashboard_tolerance_pct() -> float:
    return load_world_config().anomalies.dashboard_gap_tolerance_pct


# The period's reported lines under D-010 sourcing: ingested statements read from
# label, received ones from staging. `$2` narrows to one statement when not NULL.
_LINES_CTE = """
WITH scope AS (
    SELECT s.id AS statement_id, s.period AS stmt_period, s.status, d.dialect
    FROM label.statements s
    JOIN label.distributors d ON d.id = s.distributor_id
    WHERE s.period = $1 AND ($2::bigint IS NULL OR s.id = $2)
),
lines AS (
    SELECT 'label' AS source, l.id, l.statement_id, sc.stmt_period, sc.dialect,
           l.period, l.isrc, l.upc, l.store, l.territory, l.units, l.gross_amount,
           l.currency, l.line_hash
    FROM label.statement_lines l
    JOIN scope sc ON sc.statement_id = l.statement_id AND sc.status = 'ingested'
    UNION ALL
    SELECT 'staged', i.id, i.statement_id, sc.stmt_period, sc.dialect,
           i.period, i.isrc, i.upc, i.store, i.territory, i.units, i.gross_amount,
           i.currency, i.line_hash
    FROM staging.ingested_lines i
    JOIN scope sc ON sc.statement_id = i.statement_id AND sc.status = 'received'
)
"""


async def run_scan(
    source: asyncpg.Pool | asyncpg.Connection, *, period: str, statement_id: int | None = None
) -> ScanReport:
    """Run every rule; deterministic given the database state."""
    by_dialect, streaming_stores = _feed_reference()

    counts = await source.fetchrow(
        _LINES_CTE + "SELECT (SELECT count(*) FROM scope) AS n_statements, "
        "(SELECT count(*) FROM lines) AS n_lines",
        period,
        statement_id,
    )
    report = ScanReport(
        period=period,
        statement_id=statement_id,
        n_statements=counts["n_statements"],
        n_lines=counts["n_lines"],
    )
    if report.n_statements == 0:
        report.notes.append(f"no statements found for period {period}")
        return report

    def add(kind: str, row: asyncpg.Record, detail: str) -> None:
        report.candidates.append(
            ScanCandidate(
                kind=kind,
                source=row["source"],
                line_id=row["id"],
                statement_id=row["statement_id"],
                detail=detail,
            )
        )

    # ── duplicate_line ────────────────────────────────────────────────────────
    rows = await source.fetch(
        _LINES_CTE
        + """
        SELECT * FROM lines
        WHERE (statement_id, line_hash) IN (
            SELECT statement_id, line_hash FROM lines
            GROUP BY statement_id, line_hash HAVING count(*) > 1
        )
        ORDER BY statement_id, line_hash, source, id
        """,
        period,
        statement_id,
    )
    groups: dict[tuple[int, str], list[asyncpg.Record]] = {}
    for row in rows:
        groups.setdefault((row["statement_id"], row["line_hash"]), []).append(row)
    for (_, _), members in sorted(groups.items()):
        keeper, *extras = members  # ordered by (source, id): label first, lowest id kept
        for row in extras:
            add(
                "duplicate_line",
                row,
                f"exact duplicate of line {keeper['id']} (same line_hash; "
                f"{row['isrc']} {row['store']} {row['territory']} {row['period']})",
            )

    # ── unknown_isrc ──────────────────────────────────────────────────────────
    rows = await source.fetch(
        _LINES_CTE
        + """
        SELECT * FROM lines l
        WHERE l.isrc <> '' AND NOT EXISTS (SELECT 1 FROM label.tracks t WHERE t.isrc = l.isrc)
        ORDER BY source, id
        """,
        period,
        statement_id,
    )
    for row in rows:
        add(
            "unknown_isrc",
            row,
            f"ISRC {row['isrc']} matches no catalog track "
            f"({row['store']} {row['territory']}, {row['units']} units, "
            f"{row['gross_amount']} {row['currency']})",
        )

    # ── currency_mismatch ─────────────────────────────────────────────────────
    rows = await source.fetch(
        _LINES_CTE + "SELECT * FROM lines ORDER BY source, id", period, statement_id
    )
    for row in rows:
        reference = by_dialect.get(row["dialect"])
        if reference is None:  # unknown dialect — nothing to judge against
            continue
        feed_currency, gbp_territories = reference
        expected = "GBP" if row["territory"] in gbp_territories else feed_currency
        if row["currency"] != expected:
            add(
                "currency_mismatch",
                row,
                f"feed dialect {row['dialect']} reports {expected} for "
                f"{row['territory']} but the line claims {row['currency']} "
                f"({row['isrc']} {row['store']}, amount {row['gross_amount']})",
            )

    # ── negative_units ────────────────────────────────────────────────────────
    negatives = [r for r in rows if r["units"] < 0 or r["gross_amount"] < 0]
    for row in negatives:
        add(
            "negative_units",
            row,
            f"{row['units']} units / {row['gross_amount']} {row['currency']} "
            f"({row['isrc']} {row['store']} {row['territory']})",
        )

    # ── period_bleed ──────────────────────────────────────────────────────────
    for row in rows:
        if row["period"] != row["stmt_period"]:
            add(
                "period_bleed",
                row,
                f"line dated {row['period']} inside the {row['stmt_period']} statement "
                f"({row['isrc']} {row['store']})",
            )

    # ── sudden_territory_spike ────────────────────────────────────────────────
    # Zero-history filter first (indexed probes per line), median baseline only for
    # the few survivors — never a per-line aggregate over the whole history.
    spike_rows = await source.fetch(
        _LINES_CTE
        + """
        , fresh AS (
            SELECT l.* FROM lines l
            WHERE l.store = ANY($3::text[]) AND l.isrc <> '' AND l.units > 0
              AND NOT EXISTS (
                  SELECT 1 FROM label.statement_lines h
                  WHERE h.isrc = l.isrc AND h.period < $1 AND h.store = l.store
                    AND h.territory = l.territory AND h.units > 0
              )
        )
        SELECT f.*, (
            SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY h.units)
            FROM label.statement_lines h
            WHERE h.isrc = f.isrc AND h.period < $1 AND h.store = f.store AND h.units > 0
        ) AS median_units
        FROM fresh f
        ORDER BY f.source, f.id
        """,
        period,
        statement_id,
        list(streaming_stores),
    )
    for row in spike_rows:
        if row["median_units"] is None:
            continue  # no history at all for (isrc, store) — no baseline to spike against
        median_units = float(row["median_units"])
        ratio = row["units"] / median_units if median_units > 0 else float("inf")
        where = (
            f"first {row['territory']} activity for {row['isrc']} on {row['store']}: "
            f"{row['units']} units vs median {median_units:.0f} ({ratio:.1f}x)"
        )
        if ratio >= SPIKE_FACTOR:
            add(
                "sudden_territory_spike",
                row,
                f"{where} — zero prior history in the territory "
                f"(threshold {SPIKE_FACTOR:.0f}x; artificial-streaming smell)",
            )
        else:
            report.within_tolerance.append(
                f"new-territory line inside tolerance ({row['source']} line {row['id']}): "
                f"{where} < {SPIKE_FACTOR:.0f}x — not flagged"
            )

    # ── dashboard_gap ─────────────────────────────────────────────────────────
    tolerance = _dashboard_tolerance_pct()
    has_dashboard = await source.fetchval(
        "SELECT EXISTS (SELECT 1 FROM label.dashboard_streams WHERE period = $1)", period
    )
    if not has_dashboard:
        report.notes.append(
            f"no dashboard reference rows for {period} — dashboard_gap check skipped"
        )
    else:
        gap_rows = await source.fetch(
            # The dashboard aggregates by the *statement's* period (late-reported lines
            # count where they were reported), matching how the reference is built.
            _LINES_CTE
            + """
            , stmt_totals AS (
                SELECT isrc, store, sum(units) AS units
                FROM lines WHERE store = ANY($3::text[]) AND isrc <> ''
                GROUP BY isrc, store
            ),
            anchors AS (
                SELECT DISTINCT ON (isrc, store) isrc, store, source, id, statement_id
                FROM lines WHERE store = ANY($3::text[]) AND isrc <> ''
                ORDER BY isrc, store, units DESC, id DESC
            )
            SELECT t.isrc, t.store, t.units AS stmt_units, d.streams AS dashboard_units,
                   a.source, a.id, a.statement_id
            FROM stmt_totals t
            JOIN label.dashboard_streams d ON d.period = $1 AND d.isrc = t.isrc
                                           AND d.store = t.store
            JOIN anchors a ON a.isrc = t.isrc AND a.store = t.store
            WHERE t.units > 0 AND d.streams <> t.units
            ORDER BY t.isrc, t.store
            """,
            period,
            statement_id,
            list(streaming_stores),
        )
        for row in gap_rows:
            gap_pct = abs(row["dashboard_units"] - row["stmt_units"]) / row["stmt_units"] * 100
            where = (
                f"{row['isrc']}/{row['store']}: dashboard {row['dashboard_units']} vs "
                f"statement total {row['stmt_units']} ({gap_pct:.1f}% gap)"
            )
            if gap_pct > tolerance:
                add(
                    "dashboard_gap",
                    row,
                    f"{where} beyond {tolerance:.0f}% tolerance — anchored to the "
                    f"largest contributing line; statement money stays authoritative",
                )
            else:
                report.within_tolerance.append(
                    f"dashboard divergence inside tolerance: {where} ≤ {tolerance:.0f}% "
                    f"— not flagged"
                )

    report.candidates.sort(key=lambda c: (c.kind, c.source, c.line_id))
    return report


class ScanAnomaliesParams(BaseModel):
    model_config = ConfigDict(frozen=True)

    period: str = Field(pattern=r"^\d{4}-\d{2}$", description="statement period to scan")
    statement_id: int | None = Field(
        default=None, description="narrow the scan to one statement (default: whole period)"
    )


def _render(report: ScanReport) -> str:
    scope = f"statement {report.statement_id}" if report.statement_id else "all statements"
    out = [
        f"Anomaly scan — period {report.period}, {scope}: "
        f"{report.n_statements} statement(s), {report.n_lines} lines, "
        f"{len(report.candidates)} candidate flag(s)."
    ]
    for note in report.notes:
        out.append(f"Note: {note}")
    for kind, members in sorted(report.by_kind().items()):
        severity = SEVERITY_BY_KIND.get(kind, "warning")
        out.append(f"\n{kind} ({severity}) — {len(members)} candidate(s):")
        for candidate in members[:_SHOWN_PER_KIND]:
            line_ref = (
                f"line {candidate.line_id}"
                if candidate.source == "label"
                else f"staged line {candidate.line_id}"
            )
            out.append(f"  {line_ref} (statement {candidate.statement_id}): {candidate.detail}")
        if len(members) > _SHOWN_PER_KIND:
            out.append(f"  … and {len(members) - _SHOWN_PER_KIND} more")
    if report.within_tolerance:
        out.append("\nMeasured but within tolerance (do NOT flag):")
        out.extend(f"  {line}" for line in report.within_tolerance[:_SHOWN_PER_KIND])
        if len(report.within_tolerance) > _SHOWN_PER_KIND:
            out.append(f"  … and {len(report.within_tolerance) - _SHOWN_PER_KIND} more")
    label_excl, staged_excl = report.suggested_exclusions()
    if label_excl or staged_excl:
        out.append("\nSuggested exclusions for compute_allocations / calc_royalties")
        out.append("(corrupt reporting; dashboard_gap lines stay in — statement is authoritative):")
        if label_excl:
            out.append(f"  exclude_line_ids={label_excl}")
        if staged_excl:
            out.append(f"  exclude_staged_line_ids={staged_excl}")
    elif not report.candidates:
        out.append("No anomalies detected by the rule set.")
    return "\n".join(out)


def build_scan_anomalies_tool(ctx: ToolContext) -> Tool[ScanAnomaliesParams]:
    async def handler(params: ScanAnomaliesParams) -> str:
        report = await run_scan(ctx.pool, period=params.period, statement_id=params.statement_id)
        return _render(report)

    return Tool(
        name="scan_anomalies",
        description=(
            "Run the deterministic anomaly rules over a period's reported lines "
            "(ingested + staged): duplicates, unknown ISRCs, currency mismatches, "
            "negative units, period bleed, territory spikes (vs history), and "
            "dashboard gaps (vs the 5% tolerance). Returns candidate flags with "
            "evidence, within-tolerance measurements that must NOT be flagged, and "
            "suggested line exclusions per source. Review candidates before flagging "
            "— you own the final list."
        ),
        params=ScanAnomaliesParams,
        handler=handler,
    )
