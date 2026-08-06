"""Pure rendering units for agent-facing tool output (no DB, no models).

Rates render as plain decimal percentages everywhere an agent can read them —
``Decimal.normalize()`` alone turns 10% into ``1E+1``, which live runs showed agents
treating as gibberish (Phase 6 verification, finding 2).

Snippets are query-aware: a hit's 240-char excerpt centers on the region matching the
query instead of the clause head. Rate cards open with ~140 chars of boilerplate, so a
head-anchored snippet structurally hid every revenue-type line past the second — the
mechanism behind the Beatriz Romano sync-rate false abstention (finding 1).
"""

from decimal import Decimal

from backline.royaltycalc import pct
from backline.tools.retrieval import query_snippet

RATE_CLAUSE = (
    "In consideration of the rights granted herein, Label shall credit Artist's "
    "royalty account with the following percentages of Net Receipts:\n"
    "(a1) 18% of Net Receipts from interactive audio streaming throughout the Territory;\n"
    "(a2) 20% of Net Receipts from permanent digital downloads throughout the Territory;\n"
    "(a3) 54% of Net Receipts from synchronization licensing throughout the Territory;\n"
    "(a4) 15% of Net Receipts from physical product (vinyl, compact disc) throughout "
    "the Territory;"
)


def test_calc_pct_never_uses_scientific_notation() -> None:
    # calc_royalties renders rates via the one shared formatter (D-030) — the same
    # `pct` the contract corpus uses, so tool output and quoted clauses can't diverge.
    assert pct(Decimal("0.1")) == "10%"
    assert pct(Decimal("0.2")) == "20%"
    assert pct(Decimal("0.3")) == "30%"
    assert pct(Decimal("0.54")) == "54%"
    assert pct(Decimal("0.225")) == "22.5%"


def test_snippet_short_content_passes_through() -> None:
    assert query_snippet("a short clause", "anything") == "a short clause"


def test_snippet_centers_on_the_query_region() -> None:
    # 'sync' must light up 'synchronization' (prefix match) even though the sync line
    # sits past the 240-char head of the clause.
    snippet = query_snippet(RATE_CLAUSE, "sync rate")
    assert "synchronization licensing" in snippet
    assert "54%" in snippet
    assert snippet.startswith("…")  # the head was cut, and says so


def test_snippet_without_matches_falls_back_to_the_head() -> None:
    snippet = query_snippet(RATE_CLAUSE, "zzqx gibberish")
    assert snippet.startswith("In consideration")
    assert snippet.endswith("…")
    assert len(snippet) <= 241


def test_snippet_is_deterministic_and_bounded() -> None:
    once = query_snippet(RATE_CLAUSE, "physical product")
    again = query_snippet(RATE_CLAUSE, "physical product")
    assert once == again
    assert "physical product" in once
    assert len(once) <= 242  # limit plus at most two ellipsis marks
