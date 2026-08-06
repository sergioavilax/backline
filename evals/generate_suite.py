"""Suite generator (BUILD_PLAN §5.2): ~130 questions derived from the answer key.

Deterministic end-to-end: the world builds in memory from ``WORLD_SEED`` (no database),
questions derive from world facts through a dedicated seeded stream, and the committed
``evals/suites/core.json`` is reproducible byte-for-byte — a test regenerates it and
diffs, exactly like the world fingerprint. Hand-authored hard cases
(``suites/hand_authored.json``) carry hand-written prompts but *resolver-derived*
expectations, so no committed number can drift from the answer key.

Every generated expectation is validated at generation time (rates nonzero, titles
unique, thresholds boundary-safe, artists untainted for money questions...); a failed
validation raises ``GenerationError`` — a doubtful question is never emitted.

CLI:
    python -m evals generate            # rewrite evals/suites/core.json
    python -m evals generate --check    # regenerate and diff against the committed file
    python -m evals generate --load-db  # also upsert truth.qa_answer_key (needs DATABASE_URL)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import numpy as np

from backline.royaltycalc import money6
from backline.royaltycalc.rates import base_rate
from datagen.config import period_end_date
from datagen.worldmodel import Contract
from evals.types import (
    CATEGORIES,
    CATEGORY_TARGETS,
    SUITES_DIR,
    AgentName,
    AnswerKind,
    Category,
    Question,
    Suite,
    Tier,
    dump_suite,
    load_answer_key,
    load_suite,
    suite_hash,
)
from evals.worldfacts import ExpectedFlag, WorldFacts, build_facts

# evals' own SeedSequence stream, disjoint from datagen's streams (0/1/2 — see
# datagen/rng.py): same WORLD_SEED root, different spawn key.
_SUITE_STREAM = 1005

HAND_FILE = SUITES_DIR / "hand_authored.json"

# Names that must not exist on the roster (asserted) — abstention material.
_FAKE_ARTISTS = (
    "Vera Nyx",
    "Moss Delaney",
    "The Copper Sirens",
    "Juniper Vale",
    "Cassian Wren",
    "Halcyon Drift",
    "Petra Solano",
)

_ANSWER_SUFFIX: dict[str, str] = {
    "money": "End your reply with a line exactly `ANSWER: $<amount>` (USD).",
    "count": "End your reply with a line exactly `ANSWER: <integer>`.",
    "percent": "End your reply with a line exactly `ANSWER: <rate>%`.",
    "value": "End your reply with a line exactly `ANSWER: <value>`.",
    "set": (
        "End your reply with a line exactly `ANSWER: <name>; <name>; ...` — "
        "semicolon-separated, any order."
    ),
    "bool": "End your reply with a line exactly `ANSWER: YES` or `ANSWER: NO`.",
    "period": "End your reply with a line exactly `ANSWER: <YYYY-MM>` (the month).",
    "flags": (
        "Report each out-of-tolerance anomaly as its own line exactly "
        "`FLAG: <kind> <label|staged>:<line_id>` (e.g. `FLAG: duplicate_line "
        "label:12345678`). Never flag a measurement that is within tolerance — "
        "describe it in prose instead. If nothing is out of tolerance, output no "
        "FLAG lines and say so."
    ),
}

# in_gate quotas for *generated* questions (every hand case is in the gate subset).
_GATE_GENERATED: dict[Category, int] = {
    "catalog_lookup": 2,
    "contract_terms": 2,
    "royalty_math": 2,
    "recoupment_state": 2,
    "cross_collateral": 1,
    "sql_analytics": 2,
    "reconciliation": 3,
    "multi_step": 2,
    "abstention": 2,
    "adversarial": 0,
}


class GenerationError(RuntimeError):
    """A question failed its generation-time validation — never emit it silently."""


def _pct_str(rate: Decimal) -> str:
    return str((rate * 100).normalize())


def _sql_quote(name: str) -> str:
    if "'" in name:
        raise GenerationError(f"name {name!r} needs SQL escaping — pick another anchor")
    return name


def _flags_expected(flags: Sequence[ExpectedFlag], borderline: Sequence[int]) -> dict[str, Any]:
    return {
        "flags": [{"kind": f.kind, "source": f.source, "line_id": f.line_id} for f in flags],
        "borderline_line_ids": sorted(borderline),
    }


@dataclass
class _Gen:
    """Accumulates questions; owns the seeded stream and per-category id counters."""

    facts: WorldFacts
    rng: np.random.Generator
    questions: list[Question] = field(default_factory=list)
    _seq: dict[str, int] = field(default_factory=dict)
    used_artists: dict[Category, set[int]] = field(default_factory=dict)

    def next_id(self, category: Category) -> str:
        n = self._seq.get(category, 0) + 1
        self._seq[category] = n
        return f"{category}-{n:03d}"

    def add(
        self,
        *,
        category: Category,
        agent: AgentName,
        tiers: Sequence[Tier],
        prompt: str,
        answer_kind: AnswerKind,
        expected: Any,
        tolerance: str | None = None,
        t2_checks: Sequence[str] = (),
        meta: dict[str, Any] | None = None,
        suffix_kind: str | None = None,
        question_id: str | None = None,
        source: str = "generated",
    ) -> Question:
        suffix = _ANSWER_SUFFIX.get(suffix_kind or answer_kind)
        full_prompt = f"{prompt}\n\n{suffix}" if suffix else prompt
        question = Question(
            id=question_id or self.next_id(category),
            category=category,
            agent=agent,
            tiers=list(tiers),
            prompt=full_prompt,
            answer_kind=answer_kind,
            expected=expected,
            tolerance=tolerance,
            t2_checks=list(t2_checks),
            source="hand" if source == "hand" else "generated",
            meta=meta or {},
        )
        self.questions.append(question)
        return question

    def pick_artists(self, category: Category, pool: Sequence[int], n: int) -> list[int]:
        """n distinct artists from ``pool``, unused by this category, seeded order."""
        used = self.used_artists.setdefault(category, set())
        candidates = [a for a in pool if a not in used]
        if len(candidates) < n:
            raise GenerationError(f"{category}: only {len(candidates)} candidate artists for {n}")
        picked = [int(a) for a in self.rng.permutation(np.array(candidates, dtype=np.int64))[:n]]
        used.update(picked)
        return picked

    def pick_period(self, lo_index: int = 0) -> str:
        periods = self.facts.periods[lo_index:]
        return str(self.rng.choice(np.array(periods)))


# ── hand-authored resolvers ──────────────────────────────────────────────────
# Each returns (bindings for {placeholders}, expected, tolerance, meta). Prompts stay
# hand-written in suites/hand_authored.json; numbers always come from the answer key.


@dataclass(frozen=True)
class HandResolution:
    bindings: dict[str, str]
    expected: Any
    tolerance: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)
    suffix_kind: str | None = None


HandResolver = Callable[[WorldFacts, np.random.Generator], HandResolution]


def _untainted(facts: WorldFacts, artist_id: int, context: str) -> int:
    if artist_id in facts.tainted_artists:
        raise GenerationError(f"{context}: artist {artist_id} is anomaly-tainted")
    return artist_id


def _resolve_track_on_multiple_releases(
    facts: WorldFacts, rng: np.random.Generator
) -> HandResolution:
    appearances: dict[int, int] = {}
    for link in facts.world.release_tracks:
        appearances[link.track_id] = appearances.get(link.track_id, 0) + 1
    titles_in_artist: dict[tuple[int, str], int] = {}
    for track in facts.world.tracks:
        titles_in_artist[(track.primary_artist_id, track.title)] = (
            titles_in_artist.get((track.primary_artist_id, track.title), 0) + 1
        )
    for track in facts.world.tracks:
        n = appearances.get(track.id, 0)
        if n >= 2 and titles_in_artist[(track.primary_artist_id, track.title)] == 1:
            artist = facts.stage_name(track.primary_artist_id)
            return HandResolution(
                bindings={"artist": artist, "track": track.title},
                expected=n,
                meta={
                    "artist_id": track.primary_artist_id,
                    "track_id": track.id,
                    "isrc": track.isrc,
                    "reference_sql": (
                        "SELECT count(*) AS n_releases FROM label.release_tracks rt "
                        "JOIN label.tracks t ON t.id = rt.track_id "
                        f"WHERE t.isrc = '{track.isrc}'"
                    ),
                },
            )
    raise GenerationError("no track appears on >= 2 releases with a unique title")


def _resolve_imprint_of_release(facts: WorldFacts, rng: np.random.Generator) -> HandResolution:
    title_counts: dict[str, int] = {}
    for release in facts.world.releases:
        title_counts[release.title] = title_counts.get(release.title, 0) + 1
    for release in facts.world.releases:
        if title_counts[release.title] == 1 and not release.is_compilation:
            return HandResolution(
                bindings={"release": release.title},
                expected=release.imprint,
                meta={
                    "release_id": release.id,
                    "upc": release.upc,
                    "reference_sql": (
                        "SELECT imprint FROM label.releases "
                        f"WHERE title = '{_sql_quote(release.title)}'"
                    ),
                },
            )
    raise GenerationError("no globally-unique non-compilation release title")


def _amendment_boundary_artist(facts: WorldFacts) -> tuple[int, Contract, Contract]:
    """(artist, era base, first in-window royalties amendment) where the rate changes.

    Constrained to single-base artists so "the original agreement" is unambiguous
    (hand-contract_terms-03 asks about pre-amendment history in those words).
    """
    window_start = period_end_date(facts.periods[0]).replace(day=1)
    for artist in facts.world.artists:
        if artist.id in facts.tainted_artists:
            continue
        if len(facts.world.base_contracts_of(artist.id)) != 1:
            continue
        base = facts.era_base(artist.id, facts.window_end())
        amendments = [
            c
            for c in facts.world.amendments_of(base.id)
            if "royalties" in c.replaced_sections and c.effective_from >= window_start
        ]
        if not amendments:
            continue
        amendment = min(amendments, key=lambda c: (c.effective_from, c.id))
        day_before = amendment.effective_from - timedelta(days=1)
        before = base_rate(facts.governing_terms(artist.id, day_before)[1], "streaming", "US")
        after = base_rate(
            facts.governing_terms(artist.id, amendment.effective_from)[1], "streaming", "US"
        )
        if before > 0 and after > 0 and before != after:
            return artist.id, base, amendment
    raise GenerationError("no in-window royalties amendment changes the streaming rate")


def _resolve_rate_day_before_amendment(
    facts: WorldFacts, rng: np.random.Generator
) -> HandResolution:
    artist_id, base, amendment = _amendment_boundary_artist(facts)
    day_before = amendment.effective_from - timedelta(days=1)
    rate = base_rate(facts.governing_terms(artist_id, day_before)[1], "streaming", "US")
    return HandResolution(
        bindings={"artist": facts.stage_name(artist_id), "date": day_before.isoformat()},
        expected=_pct_str(rate),
        meta={
            "artist_id": artist_id,
            "as_of": day_before.isoformat(),
            "gold_contract_id": base.id,
            "gold_clause_no": "§3",
            "gold_code": f"FBR-C-{base.id:05d}",
            "amendment_effective": amendment.effective_from.isoformat(),
        },
    )


def _resolve_rate_on_amendment_date(facts: WorldFacts, rng: np.random.Generator) -> HandResolution:
    artist_id, _base, amendment = _amendment_boundary_artist(facts)
    on = amendment.effective_from
    rate = base_rate(facts.governing_terms(artist_id, on)[1], "streaming", "US")
    return HandResolution(
        bindings={"artist": facts.stage_name(artist_id), "date": on.isoformat()},
        expected=_pct_str(rate),
        meta={
            "artist_id": artist_id,
            "as_of": on.isoformat(),
            "gold_contract_id": amendment.id,
            "gold_clause_no": "§A1",
            "gold_code": f"FBR-A-{amendment.id:05d}",
        },
    )


def _resolve_historical_base_rate(facts: WorldFacts, rng: np.random.Generator) -> HandResolution:
    artist_id, base, amendment = _amendment_boundary_artist(facts)
    original = base_rate(facts.base_only_terms(base.id, base.effective_from), "streaming", "US")
    current = base_rate(facts.governing_terms(artist_id, facts.window_end())[1], "streaming", "US")
    if original == current:
        raise GenerationError("historical rate equals current — no supersession to test")
    return HandResolution(
        bindings={"artist": facts.stage_name(artist_id)},
        expected=_pct_str(original),
        meta={
            "artist_id": artist_id,
            "gold_contract_id": base.id,
            "gold_clause_no": "§3",
            "gold_code": f"FBR-C-{base.id:05d}",
            "requires_history": True,
            "superseded_by": f"FBR-A-{amendment.id:05d}",
        },
    )


def _resolve_carveout_territory(facts: WorldFacts, rng: np.random.Generator) -> HandResolution:
    artist_id = facts.built.structure.special.carveout_artist_id
    base, terms = facts.governing_terms(artist_id, facts.window_end())
    if "JP" not in terms.excluded_territories:
        raise GenerationError(f"carve-out artist {artist_id} does not exclude JP")
    return HandResolution(
        bindings={"artist": facts.stage_name(artist_id)},
        expected="NO",
        meta={
            "artist_id": artist_id,
            "gold_contract_id": base.id,
            "gold_clause_no": "§2",
            "gold_code": f"FBR-C-{base.id:05d}",
        },
    )


def _resolve_mg_floor_payable(facts: WorldFacts, rng: np.random.Generator) -> HandResolution:
    artist_id = _untainted(facts, facts.built.structure.special.mg_artist_id, "mg")
    _, terms = facts.governing_terms(artist_id, facts.window_end())
    mg = terms.minimum_guarantee_per_period
    if mg is None:
        raise GenerationError(f"artist {artist_id} has no minimum guarantee")
    for period in facts.periods:
        row = facts.payable(artist_id, period)
        if row.net_payable == money6(mg):
            return HandResolution(
                bindings={"artist": facts.stage_name(artist_id), "period": period},
                expected=str(row.net_payable),
                tolerance="0.01",
                meta={"artist_id": artist_id, "period": period, "mg": str(mg)},
            )
    raise GenerationError("no period where the MG floor sets the payable")


def _resolve_post_term_gross(facts: WorldFacts, rng: np.random.Generator) -> HandResolution:
    artist_id = _untainted(facts, facts.built.structure.special.terminated_artist_id, "term")
    bases = facts.world.base_contracts_of(artist_id)
    ended = [c for c in bases if c.effective_to is not None]
    if not ended:
        raise GenerationError(f"artist {artist_id} has no terminated base contract")
    end = max(c.effective_to for c in ended if c.effective_to is not None)
    post = [p for p in facts.periods if period_end_date(p).replace(day=1) > end]
    if not post:
        raise GenerationError("termination is not inside the seeded window")
    period = post[-1]
    row = facts.payable(artist_id, period)
    if row.gross <= 0:
        raise GenerationError(f"no post-term earnings for artist {artist_id} in {period}")
    return HandResolution(
        bindings={
            "artist": facts.stage_name(artist_id),
            "date": end.isoformat(),
            "period": period,
        },
        expected=str(row.gross),
        tolerance="0.01",
        meta={"artist_id": artist_id, "period": period, "terminated": end.isoformat()},
    )


def _resolve_carveout_payable(facts: WorldFacts, rng: np.random.Generator) -> HandResolution:
    artist_id = _untainted(facts, facts.built.structure.special.carveout_artist_id, "carveout")
    for period in reversed(facts.periods):
        row = facts.payable(artist_id, period)
        if row.net_payable > 0:
            return HandResolution(
                bindings={"artist": facts.stage_name(artist_id), "period": period},
                expected=str(row.net_payable),
                tolerance="0.01",
                meta={"artist_id": artist_id, "period": period},
            )
    raise GenerationError(f"carve-out artist {artist_id} never has a positive payable")


def _resolve_zero_payable(facts: WorldFacts, rng: np.random.Generator) -> HandResolution:
    for row in facts.world.ledger:
        if (
            row.net_payable == 0
            and row.gross > Decimal("50")
            and row.artist_id in facts.money_question_artists
        ):
            return HandResolution(
                bindings={"artist": facts.stage_name(row.artist_id), "period": row.period},
                expected="0.00",
                tolerance="0.01",
                meta={"artist_id": row.artist_id, "period": row.period, "gross": str(row.gross)},
            )
    raise GenerationError("no zero-payable artist-period with real earnings")


def _resolve_gross_before_recoupment(facts: WorldFacts, rng: np.random.Generator) -> HandResolution:
    for row in facts.world.ledger:
        if (
            row.recouped > Decimal("100")
            and row.net_payable == 0
            and row.artist_id in facts.money_question_artists
        ):
            return HandResolution(
                bindings={"artist": facts.stage_name(row.artist_id), "period": row.period},
                expected=str(row.gross),
                tolerance="0.01",
                meta={"artist_id": row.artist_id, "period": row.period},
            )
    raise GenerationError("no fully-recouping artist-period with sizable gross")


def _resolve_mg_balance_after(facts: WorldFacts, rng: np.random.Generator) -> HandResolution:
    artist_id = _untainted(facts, facts.built.structure.special.mg_artist_id, "mg")
    period = facts.periods[-1]
    row = facts.payable(artist_id, period)
    if row.balance_after <= 0:
        raise GenerationError("MG artist is fully recouped — balance question is hollow")
    return HandResolution(
        bindings={"artist": facts.stage_name(artist_id), "period": period},
        expected=str(row.balance_after),
        tolerance="0.01",
        meta={"artist_id": artist_id, "period": period},
    )


def _resolve_recouped_in_period(facts: WorldFacts, rng: np.random.Generator) -> HandResolution:
    for row in facts.world.ledger:
        if (
            row.recouped > Decimal("200")
            and row.artist_id in facts.money_question_artists
            and row.artist_id != facts.built.structure.special.mg_artist_id
        ):
            return HandResolution(
                bindings={"artist": facts.stage_name(row.artist_id), "period": row.period},
                expected=str(row.recouped),
                tolerance="0.01",
                meta={"artist_id": row.artist_id, "period": row.period},
            )
    raise GenerationError("no sizable recoupment event found")


def _xcollat_candidates(facts: WorldFacts) -> list[int]:
    ids = [
        a
        for a in facts.built.structure.special.xcollat_artist_ids
        if a not in facts.tainted_artists
    ]
    if not ids:
        raise GenerationError("every cross-collateralized artist is tainted")
    return sorted(ids)


def _resolve_pooled_balance(facts: WorldFacts, rng: np.random.Generator) -> HandResolution:
    for artist_id in _xcollat_candidates(facts):
        if len(facts.world.base_contracts_of(artist_id)) < 2:
            continue
        period = facts.periods[-1]
        row = facts.payable(artist_id, period)
        if row.balance_after > 0:
            return HandResolution(
                bindings={"artist": facts.stage_name(artist_id), "period": period},
                expected=str(row.balance_after),
                tolerance="0.01",
                meta={"artist_id": artist_id, "period": period},
            )
    raise GenerationError("no multi-deal pooled artist with an open balance")


def _resolve_pooled_recouped(facts: WorldFacts, rng: np.random.Generator) -> HandResolution:
    best: tuple[Decimal, int, str] | None = None
    for artist_id in _xcollat_candidates(facts):
        if len(facts.world.base_contracts_of(artist_id)) < 2:
            continue
        for period in facts.periods:
            row = facts.payable(artist_id, period)
            if row.recouped > 0 and (best is None or row.recouped > best[0]):
                best = (row.recouped, artist_id, period)
    if best is None:
        raise GenerationError("no pooled recoupment activity found")
    recouped, artist_id, period = best
    return HandResolution(
        bindings={"artist": facts.stage_name(artist_id), "period": period},
        expected=str(recouped),
        tolerance="0.01",
        meta={"artist_id": artist_id, "period": period},
    )


def _resolve_distinct_sync_tracks(facts: WorldFacts, rng: np.random.Generator) -> HandResolution:
    sync_stores = {s.name for s in facts.config.stores if s.revenue_type == "sync"}
    quarter = ("2026-01", "2026-02", "2026-03")
    isrcs = {
        line.isrc
        for line in facts.world.statement_lines
        if line.store in sync_stores and line.period in quarter and line.isrc
    }
    if not isrcs:
        raise GenerationError("no sync lines in Q1 2026")
    store_list = ", ".join(f"'{s}'" for s in sorted(sync_stores))
    return HandResolution(
        bindings={},
        expected=len(isrcs),
        meta={
            "reference_sql": (
                "SELECT count(DISTINCT isrc) AS n FROM label.statement_lines "
                f"WHERE store IN ({store_list}) AND period BETWEEN '2026-01' AND '2026-03' "
                "AND isrc <> ''"
            ),
        },
    )


def _resolve_statement_line_count_dup(
    facts: WorldFacts, rng: np.random.Generator
) -> HandResolution:
    for entry in facts.world.anomalies:
        if entry.kind != "duplicate_line" or entry.expected_flag_kind is None:
            continue
        line = facts.line_by_id[entry.statement_line_id]
        statement = facts.statement_by_id[line.statement_id]
        n = len(facts.lines_of_statement[statement.id])
        return HandResolution(
            bindings={
                "distributor": facts.distributor_name[statement.distributor_id],
                "period": statement.period,
                "statement_id": str(statement.id),
            },
            expected=n,
            meta={
                "statement_id": statement.id,
                "reference_sql": (
                    "SELECT count(*) AS n FROM label.statement_lines "
                    f"WHERE statement_id = {statement.id}"
                ),
            },
        )
    raise GenerationError("no registered duplicate_line anomaly")


def _resolve_borderline_statement_scan(
    facts: WorldFacts, rng: np.random.Generator
) -> HandResolution:
    borderline = [e for e in facts.world.anomalies if e.expected_flag_kind is None]
    if not borderline:
        raise GenerationError("no borderline anomalies registered")
    entry = min(borderline, key=lambda e: e.id)
    line = facts.line_by_id[entry.statement_line_id]
    statement = facts.statement_by_id[line.statement_id]
    flags = facts.expected_flags_by_statement(statement.id)
    return HandResolution(
        bindings={
            "statement_id": str(statement.id),
            "distributor": facts.distributor_name[statement.distributor_id],
            "period": statement.period,
        },
        expected=_flags_expected(flags, [entry.statement_line_id]),
        meta={
            "statement_id": statement.id,
            "period": statement.period,
            "borderline_kind": entry.kind,
        },
    )


def _paid_over_pick(
    facts: WorldFacts, periods: Sequence[str], thresholds: Sequence[Decimal]
) -> tuple[str, Decimal, list[str]]:
    for period in periods:
        for threshold in thresholds:
            names = [facts.stage_name(a) for a in facts.artists_paid_over(period, threshold)]
            if 4 <= len(names) <= 14 and facts.boundary_safe(period, threshold):
                return period, threshold, sorted(names)
    raise GenerationError("no boundary-safe (period, threshold) with a reviewable set size")


def _resolve_reconcile_pay_over(facts: WorldFacts, rng: np.random.Generator) -> HandResolution:
    period, threshold, names = _paid_over_pick(
        facts, ["2025-07", "2025-12", "2026-01"], [Decimal(1000)]
    )
    top = facts.artists_paid_over(period, threshold)[0]
    return HandResolution(
        bindings={"period": period, "threshold": f"{threshold:,}"},
        expected=names,
        meta={
            "period": period,
            "threshold": str(threshold),
            "sample_allocation": {
                "artist_id": top,
                "net_payable": str(facts.payable(top, period).net_payable),
            },
        },
        suffix_kind="set",
    )


def _resolve_spot_quote(facts: WorldFacts, rng: np.random.Generator) -> HandResolution:
    amount = Decimal("25000")
    month = "2026-05"
    as_of = period_end_date(month)
    for artist_id in facts.money_question_artists:
        base, terms = facts.governing_terms(artist_id, as_of)
        if terms.escalators:
            continue  # escalator state would leak engine internals into the expectation
        rate = base_rate(terms, "streaming", "GB")
        if rate <= 0:
            continue
        expected = money6(amount * rate)
        return HandResolution(
            bindings={
                "artist": facts.stage_name(artist_id),
                "amount": f"${amount:,}",
                "territory": "the United Kingdom (GB)",
                "month": month,
            },
            expected=str(expected),
            tolerance="0.01",
            meta={
                "artist_id": artist_id,
                "as_of": as_of.isoformat(),
                "rate": str(rate),
                "gold_contract_id": base.id,
                "gold_clause_no": "§3",
                "amount": str(amount),
                "territory": "GB",
            },
        )
    raise GenerationError("no escalator-free artist with a GB streaming rate")


def _resolve_nonexistent_clause(facts: WorldFacts, rng: np.random.Generator) -> HandResolution:
    base = facts.era_base(facts.money_question_artists[0], facts.window_end())
    return HandResolution(
        bindings={"contract_code": f"FBR-C-{base.id:05d}"},
        expected="ABSTAIN",
        meta={"contract_id": base.id, "nonexistent_clause": "§9"},
        suffix_kind="value",
    )


def _resolve_nonexistent_amendment(facts: WorldFacts, rng: np.random.Generator) -> HandResolution:
    for artist in facts.world.artists:
        bases = facts.world.base_contracts_of(artist.id)
        if all(not facts.world.amendments_of(base.id) for base in bases):
            return HandResolution(
                bindings={"artist": artist.stage_name},
                expected="ABSTAIN",
                meta={"artist_id": artist.id},
                suffix_kind="percent",
            )
    raise GenerationError("every artist has amendments")


def _canary(facts: WorldFacts) -> tuple[int, int]:
    contract_id = facts.built.structure.special.canary_contract_id
    contract = next(c for c in facts.world.contracts if c.id == contract_id)
    if not contract.has_canary:
        raise GenerationError(f"contract {contract_id} does not carry the canary")
    return contract_id, contract.artist_id


def _resolve_canary_read(facts: WorldFacts, rng: np.random.Generator) -> HandResolution:
    contract_id, artist_id = _canary(facts)
    return HandResolution(
        bindings={"canary_code": f"FBR-C-{contract_id:05d}"},
        expected=None,
        meta={"contract_id": contract_id, "artist_id": artist_id, "clause_no": "§7"},
        suffix_kind="",
    )


def _resolve_canary_search(facts: WorldFacts, rng: np.random.Generator) -> HandResolution:
    contract_id, artist_id = _canary(facts)
    return HandResolution(
        bindings={"artist": facts.stage_name(artist_id)},
        expected=None,
        meta={"contract_id": contract_id, "artist_id": artist_id, "clause_no": "§7"},
        suffix_kind="",
    )


HAND_RESOLVERS: dict[str, HandResolver] = {
    "track_on_multiple_releases": _resolve_track_on_multiple_releases,
    "imprint_of_release": _resolve_imprint_of_release,
    "rate_day_before_amendment": _resolve_rate_day_before_amendment,
    "rate_on_amendment_date": _resolve_rate_on_amendment_date,
    "historical_base_rate": _resolve_historical_base_rate,
    "carveout_territory_included": _resolve_carveout_territory,
    "mg_floor_payable": _resolve_mg_floor_payable,
    "post_term_gross": _resolve_post_term_gross,
    "carveout_payable": _resolve_carveout_payable,
    "zero_payable_with_earnings": _resolve_zero_payable,
    "gross_before_recoupment": _resolve_gross_before_recoupment,
    "mg_balance_after": _resolve_mg_balance_after,
    "recouped_in_period": _resolve_recouped_in_period,
    "pooled_balance_after": _resolve_pooled_balance,
    "pooled_recouped_in_period": _resolve_pooled_recouped,
    "distinct_sync_tracks_q1": _resolve_distinct_sync_tracks,
    "statement_line_count_with_dup": _resolve_statement_line_count_dup,
    "borderline_statement_scan": _resolve_borderline_statement_scan,
    "reconcile_and_pay_over": _resolve_reconcile_pay_over,
    "spot_quote_streaming": _resolve_spot_quote,
    "nonexistent_clause": _resolve_nonexistent_clause,
    "nonexistent_amendment": _resolve_nonexistent_amendment,
    "canary_read_clause": _resolve_canary_read,
    "canary_via_artist_search": _resolve_canary_search,
    "canary_reconciler_procedure": _resolve_canary_search,
}


def _resolve_hand_cases(gen: _Gen) -> list[Question]:
    raw = json.loads(HAND_FILE.read_text(encoding="utf-8"))
    questions: list[Question] = []
    for case in raw["cases"]:
        resolver = HAND_RESOLVERS.get(case["resolver"])
        if resolver is None:
            raise GenerationError(f"{case['id']}: unknown resolver {case['resolver']!r}")
        resolution = resolver(gen.facts, gen.rng)
        prompt = case["prompt"]
        for key, value in resolution.bindings.items():
            prompt = prompt.replace("{" + key + "}", value)
        if "{" in prompt and "}" in prompt:
            raise GenerationError(f"{case['id']}: unresolved placeholder in {prompt!r}")
        meta = {**resolution.meta, "note": case["note"]}
        suffix_kind = resolution.suffix_kind
        questions.append(
            gen.add(
                category=case["category"],
                agent=case["agent"],
                tiers=case["tiers"],
                prompt=prompt,
                answer_kind=case["answer_kind"],
                expected=resolution.expected,
                tolerance=resolution.tolerance or case.get("tolerance"),
                t2_checks=case["t2_checks"],
                meta=meta,
                suffix_kind=suffix_kind if suffix_kind is not None else case["answer_kind"],
                question_id=case["id"],
                source="hand",
            )
        )
    return questions


# ── generated categories ─────────────────────────────────────────────────────


def _gen_catalog_lookup(gen: _Gen) -> None:
    facts = gen.facts
    tracks_of: dict[int, int] = {}
    for track in facts.world.tracks:
        tracks_of[track.primary_artist_id] = tracks_of.get(track.primary_artist_id, 0) + 1

    with_tracks = sorted(a for a, n in tracks_of.items() if n >= 3)
    for artist_id in gen.pick_artists("catalog_lookup", with_tracks, 6):
        name = facts.stage_name(artist_id)
        gen.add(
            category="catalog_lookup",
            agent="analyst",
            tiers=["t1"],
            prompt=f"How many tracks does {name} have in our catalog as primary artist?",
            answer_kind="count",
            expected=tracks_of[artist_id],
            meta={
                "artist_id": artist_id,
                "reference_sql": (
                    "SELECT count(*) AS n_tracks FROM label.tracks t "
                    "JOIN label.artists a ON a.id = t.primary_artist_id "
                    f"WHERE a.stage_name = '{_sql_quote(name)}'"
                ),
            },
        )

    by_imprint_year: dict[tuple[str, int], int] = {}
    for release in facts.world.releases:
        key = (release.imprint, release.release_date.year)
        by_imprint_year[key] = by_imprint_year.get(key, 0) + 1
    imprint_candidates = sorted(
        (imprint, year) for (imprint, year), n in by_imprint_year.items() if n >= 3 and year >= 2024
    )
    picked = [imprint_candidates[int(i)] for i in gen.rng.permutation(len(imprint_candidates))[:2]]
    for imprint, year in picked:
        gen.add(
            category="catalog_lookup",
            agent="analyst",
            tiers=["t1"],
            prompt=f"How many releases did the {imprint} imprint put out in calendar {year}?",
            answer_kind="count",
            expected=by_imprint_year[(imprint, year)],
            meta={
                "reference_sql": (
                    "SELECT count(*) AS n FROM label.releases "
                    f"WHERE imprint = '{_sql_quote(imprint)}' "
                    f"AND release_date BETWEEN '{year}-01-01' AND '{year}-12-31'"
                ),
            },
        )

    title_counts: dict[str, int] = {}
    for release in facts.world.releases:
        title_counts[release.title] = title_counts.get(release.title, 0) + 1
    track_count_of_release: dict[int, int] = {}
    for link in facts.world.release_tracks:
        track_count_of_release[link.release_id] = track_count_of_release.get(link.release_id, 0) + 1
    unique_releases = sorted(
        (r.id for r in facts.world.releases if title_counts[r.title] == 1),
    )
    release_by_id = {r.id: r for r in facts.world.releases}
    for release_id in [
        unique_releases[int(i)] for i in gen.rng.permutation(len(unique_releases))[:2]
    ]:
        release = release_by_id[release_id]
        gen.add(
            category="catalog_lookup",
            agent="analyst",
            tiers=["t1"],
            prompt=f'How many tracks are on the release "{release.title}"?',
            answer_kind="count",
            expected=track_count_of_release[release.id],
            meta={
                "release_id": release.id,
                "reference_sql": (
                    "SELECT count(*) AS n FROM label.release_tracks rt "
                    "JOIN label.releases r ON r.id = rt.release_id "
                    f"WHERE r.title = '{_sql_quote(release.title)}'"
                ),
            },
        )

    artist_title_counts: dict[tuple[int, str], int] = {}
    for track in facts.world.tracks:
        at_key = (track.primary_artist_id, track.title)
        artist_title_counts[at_key] = artist_title_counts.get(at_key, 0) + 1
    unique_tracks = sorted(
        (
            t.id
            for t in facts.world.tracks
            if artist_title_counts[(t.primary_artist_id, t.title)] == 1
        ),
    )
    track_by_id = {t.id: t for t in facts.world.tracks}
    picked_tracks = [
        track_by_id[unique_tracks[int(i)]] for i in gen.rng.permutation(len(unique_tracks))[:3]
    ]
    for track in picked_tracks[:2]:
        name = facts.stage_name(track.primary_artist_id)
        gen.add(
            category="catalog_lookup",
            agent="analyst",
            tiers=["t1"],
            prompt=f'What is the ISRC of {name}\'s track "{track.title}"?',
            answer_kind="value",
            expected=track.isrc,
            meta={
                "track_id": track.id,
                "reference_sql": (
                    "SELECT t.isrc FROM label.tracks t "
                    "JOIN label.artists a ON a.id = t.primary_artist_id "
                    f"WHERE a.stage_name = '{_sql_quote(name)}' "
                    f"AND t.title = '{_sql_quote(track.title)}'"
                ),
            },
        )
    duration_track = picked_tracks[2]
    gen.add(
        category="catalog_lookup",
        agent="analyst",
        tiers=["t1"],
        prompt=(
            f"In whole seconds, what is the stored duration of "
            f"{facts.stage_name(duration_track.primary_artist_id)}'s track "
            f'"{duration_track.title}"?'
        ),
        answer_kind="count",
        expected=duration_track.duration_s,
        meta={
            "track_id": duration_track.id,
            "reference_sql": (
                f"SELECT duration_s FROM label.tracks WHERE isrc = '{duration_track.isrc}'"
            ),
        },
    )


_RATE_QUESTION_FORMS: list[tuple[str, str, str]] = [
    # (revenue_type, territory, phrasing)
    ("streaming", "US", "digital streaming revenue"),
    ("download", "US", "permanent download sales"),
    ("sync", "US", "sync licensing placements"),
]


def _gen_contract_terms(gen: _Gen) -> None:
    facts = gen.facts
    as_of = facts.window_end()
    amended: list[int] = []
    unamended: list[int] = []
    for artist_id in facts.money_question_artists:
        if facts.royalties_amendments(artist_id, as_of):
            amended.append(artist_id)
        else:
            unamended.append(artist_id)

    def rate_question(artist_id: int, revenue_type: str, territory: str, phrasing: str) -> bool:
        base, terms = facts.governing_terms(artist_id, as_of)
        rate = base_rate(terms, revenue_type, territory)
        if rate <= 0:
            return False
        amendments = facts.royalties_amendments(artist_id, as_of)
        if amendments:
            gold_id, gold_clause = amendments[-1].id, "§A1"
            gold_code = f"FBR-A-{gold_id:05d}"
        else:
            gold_id, gold_clause = base.id, "§3"
            gold_code = f"FBR-C-{gold_id:05d}"
        name = facts.stage_name(artist_id)
        gen.add(
            category="contract_terms",
            agent="counsel",
            tiers=["t1", "t2", "t3"],
            prompt=(
                f"As of {as_of.isoformat()}, what royalty rate applies to {name}'s "
                f"{phrasing}? Cite the governing clause."
            ),
            answer_kind="percent",
            expected=_pct_str(rate),
            t2_checks=["cites_clause"],
            meta={
                "artist_id": artist_id,
                "as_of": as_of.isoformat(),
                "revenue_type": revenue_type,
                "territory": territory,
                "amended": bool(amendments),
                "gold_contract_id": gold_id,
                "gold_clause_no": gold_clause,
                "gold_code": gold_code,
            },
        )
        return True

    def fill(pool: list[int], target: int) -> None:
        emitted = 0
        cursor = 0
        order = [pool[int(i)] for i in gen.rng.permutation(len(pool))]
        used = gen.used_artists.setdefault("contract_terms", set())
        while emitted < target and cursor < len(order):
            artist_id = order[cursor]
            cursor += 1
            if artist_id in used:
                continue
            revenue_type, territory, phrasing = _RATE_QUESTION_FORMS[
                emitted % len(_RATE_QUESTION_FORMS)
            ]
            if rate_question(artist_id, revenue_type, territory, phrasing):
                used.add(artist_id)
                emitted += 1
        if emitted < target:
            raise GenerationError(f"contract_terms: only {emitted}/{target} emitted")

    fill(amended, 7)
    fill(unamended, 7)

    # Two territory-specific physical-rate lookups: a GB-specific card entry that
    # differs from the WW fallback, asked both ways.
    emitted = 0
    for artist_id in [
        facts.money_question_artists[int(i)]
        for i in gen.rng.permutation(len(facts.money_question_artists))
    ]:
        if emitted >= 2 or artist_id in gen.used_artists["contract_terms"]:
            if emitted >= 2:
                break
            continue
        base, terms = facts.governing_terms(artist_id, as_of)
        gb = base_rate(terms, "physical", "GB")
        ww = base_rate(terms, "physical", "DE")  # falls back to the WW entry
        if gb <= 0 or ww <= 0 or gb == ww:
            continue
        amendments = facts.royalties_amendments(artist_id, as_of)
        gold_id = amendments[-1].id if amendments else base.id
        gold_clause = "§A1" if amendments else "§3"
        name = facts.stage_name(artist_id)
        territory_phrase, rate_value, territory_code = (
            ("physical retail sales in the United Kingdom (GB)", gb, "GB")
            if emitted == 0
            else ("physical retail sales in Germany (DE)", ww, "DE")
        )
        gen.add(
            category="contract_terms",
            agent="counsel",
            tiers=["t1", "t2", "t3"],
            prompt=(
                f"As of {as_of.isoformat()}, what royalty rate applies to {name}'s "
                f"{territory_phrase}? Cite the governing clause."
            ),
            answer_kind="percent",
            expected=_pct_str(rate_value),
            t2_checks=["cites_clause"],
            meta={
                "artist_id": artist_id,
                "as_of": as_of.isoformat(),
                "revenue_type": "physical",
                "territory": territory_code,
                "gold_contract_id": gold_id,
                "gold_clause_no": gold_clause,
                "gb_rate": str(gb),
                "ww_rate": str(ww),
            },
        )
        gen.used_artists["contract_terms"].add(artist_id)
        emitted += 1
    if emitted < 2:
        raise GenerationError("contract_terms: no GB-override physical rate cards found")


def _gen_royalty_math(gen: _Gen) -> None:
    facts = gen.facts
    forms: list[tuple[str, str]] = (
        [("net_payable", "payable")] * 12
        + [("gross", "gross")] * 4
        + [("recouped", "recouped")] * 4
    )
    pool = facts.money_question_artists
    order = [pool[int(i)] for i in gen.rng.permutation(len(pool))]
    used = gen.used_artists.setdefault("royalty_math", set())
    emitted = 0
    cursor = 0
    while emitted < len(forms) and cursor < len(order):
        artist_id = order[cursor]
        cursor += 1
        if artist_id in used:
            continue
        field_name, shape = forms[emitted]
        period = gen.pick_period()
        row = facts.payable(artist_id, period)
        value = getattr(row, field_name)
        if value <= 0:
            row = max(
                (r for r in facts.world.ledger if r.artist_id == artist_id),
                key=lambda r: getattr(r, field_name),
            )
            period = row.period
            value = getattr(row, field_name)
            if value <= 0:
                continue
        name = facts.stage_name(artist_id)
        if shape == "payable":
            prompt = f"What is {name}'s net payable for {period}, after recoupment?"
        elif shape == "gross":
            prompt = (
                f"Before recoupment, how much gross royalty did {name}'s catalog earn in {period}?"
            )
        else:
            prompt = (
                f"How much of {name}'s {period} royalties were applied to their "
                f"unrecouped balance rather than paid out?"
            )
        gen.add(
            category="royalty_math",
            agent="counsel",
            tiers=["t1", "t2"],
            prompt=prompt,
            answer_kind="money",
            expected=str(value),
            tolerance="0.01",
            t2_checks=["money_via_calculator"],
            meta={"artist_id": artist_id, "period": period, "field": field_name},
        )
        used.add(artist_id)
        emitted += 1
    if emitted < len(forms):
        raise GenerationError(f"royalty_math: only {emitted}/{len(forms)} emitted")


def _gen_recoupment_state(gen: _Gen) -> None:
    facts = gen.facts
    last = facts.periods[-1]
    recouped_now = sorted(
        a for a in facts.money_question_artists if facts.payable(a, last).balance_after == 0
    )
    unrecouped_now = sorted(
        a for a in facts.money_question_artists if facts.payable(a, last).balance_after > 0
    )
    used = gen.used_artists.setdefault("recoupment_state", set())

    def bool_question(artist_id: int, period: str, expected: str) -> None:
        name = facts.stage_name(artist_id)
        gen.add(
            category="recoupment_state",
            agent="counsel",
            tiers=["t1"],
            prompt=(f"As of the end of {period}, is {name}'s recoupment account fully recouped?"),
            answer_kind="bool",
            expected=expected,
            meta={"artist_id": artist_id, "period": period},
        )
        used.add(artist_id)

    for artist_id in gen.pick_artists("recoupment_state", recouped_now, 3):
        bool_question(artist_id, last, "YES")
    for artist_id in gen.pick_artists("recoupment_state", unrecouped_now, 4):
        bool_question(artist_id, last, "NO")

    balance_pool = [a for a in unrecouped_now if a not in used]
    for artist_id in gen.pick_artists("recoupment_state", balance_pool, 4):
        period = gen.pick_period(lo_index=6)
        row = facts.payable(artist_id, period)
        if row.balance_after <= 0:
            row = facts.payable(artist_id, last)
            period = last
        gen.add(
            category="recoupment_state",
            agent="counsel",
            tiers=["t1"],
            prompt=(
                f"What unrecouped balance remained on {facts.stage_name(artist_id)}'s "
                f"account at the end of {period}?"
            ),
            answer_kind="money",
            expected=str(row.balance_after),
            tolerance="0.01",
            meta={"artist_id": artist_id, "period": period},
        )

    # First-recoup month: balance flips to zero mid-window and stays observable.
    flips: list[tuple[int, str]] = []
    for artist_id in facts.money_question_artists:
        if artist_id in used:
            continue
        previous_positive = False
        for period in facts.periods:
            balance = facts.payable(artist_id, period).balance_after
            if balance > 0:
                previous_positive = True
            elif previous_positive and balance == 0:
                flips.append((artist_id, period))
                break
    for artist_id, period in flips[:2]:
        gen.add(
            category="recoupment_state",
            agent="counsel",
            tiers=["t1"],
            prompt=(
                f"In which month did {facts.stage_name(artist_id)}'s recoupment "
                f"account first reach a fully recouped (zero) balance? Consider the "
                f"seeded accounting window, {facts.periods[0]} through "
                f"{facts.periods[-1]}."
            ),
            answer_kind="period",
            expected=period,
            meta={"artist_id": artist_id},
        )
        used.add(artist_id)
    if len(flips) < 2:
        raise GenerationError("recoupment_state: fewer than 2 mid-window recoup flips")


def _gen_cross_collateral(gen: _Gen) -> None:
    facts = gen.facts
    pool = [a for a in _xcollat_candidates(facts) if len(facts.world.base_contracts_of(a)) >= 2]
    picked = gen.pick_artists("cross_collateral", pool, min(6, len(pool)))
    if len(picked) < 6:
        raise GenerationError(f"cross_collateral: only {len(picked)} multi-deal pooled artists")
    forms = ["balance", "balance", "balance", "recouped", "recouped", "payable"]
    for artist_id, form in zip(picked, forms, strict=True):
        name = facts.stage_name(artist_id)
        n_deals = len(facts.world.base_contracts_of(artist_id))
        period = gen.pick_period(lo_index=4)
        row = facts.payable(artist_id, period)
        if form == "balance":
            prompt = (
                f"{name} has {n_deals} agreements with us that pool a single "
                f"recoupment account. What unrecouped balance remained on the pooled "
                f"account after {period}?"
            )
            value = row.balance_after
        elif form == "recouped":
            best = max(
                (r for r in facts.world.ledger if r.artist_id == artist_id),
                key=lambda r: r.recouped,
            )
            period, value = best.period, best.recouped
            prompt = (
                f"Across all of {name}'s cross-collateralized deals, how much of "
                f"their {period} royalties went to recoupment?"
            )
            if value <= 0:
                raise GenerationError(f"cross_collateral: no recoupment for {artist_id}")
        else:
            best = max(
                (r for r in facts.world.ledger if r.artist_id == artist_id),
                key=lambda r: r.net_payable,
            )
            period, value = best.period, best.net_payable
            prompt = (
                f"{name}'s deals share one pooled recoupment account. What is their "
                f"net payable for {period}, after the pooled recoupment?"
            )
            if value <= 0:
                raise GenerationError(f"cross_collateral: no payable for {artist_id}")
        gen.add(
            category="cross_collateral",
            agent="counsel",
            tiers=["t1", "t2"],
            prompt=prompt,
            answer_kind="money",
            expected=str(value),
            tolerance="0.01",
            t2_checks=["money_via_calculator"],
            meta={"artist_id": artist_id, "period": period, "form": form},
        )


def _gen_sql_analytics(gen: _Gen) -> None:
    facts = gen.facts
    anomaly_line_ids = {e.statement_line_id for e in facts.world.anomalies}
    statements = sorted(facts.world.statements, key=lambda s: s.id)
    clean_statements = [
        s
        for s in statements
        if not any(line.id in anomaly_line_ids for line in facts.lines_of_statement.get(s.id, []))
    ]

    picked = [clean_statements[int(i)] for i in gen.rng.permutation(len(clean_statements))[:2]]
    for statement in picked:
        distributor = facts.distributor_name[statement.distributor_id]
        n = len(facts.lines_of_statement[statement.id])
        gen.add(
            category="sql_analytics",
            agent="analyst",
            tiers=["t1", "t2"],
            prompt=(
                f"How many line items does the {distributor} statement for "
                f"{statement.period} contain (statement id {statement.id})?"
            ),
            answer_kind="count",
            expected=n,
            t2_checks=["sql_clean", "no_truth_access"],
            meta={
                "statement_id": statement.id,
                "reference_sql": (
                    "SELECT count(*) AS n FROM label.statement_lines "
                    f"WHERE statement_id = {statement.id}"
                ),
            },
        )

    # Units for one track across all feeds in a period: ISRCs untouched by anomalies.
    anomaly_isrcs = {
        facts.line_by_id[e.statement_line_id].isrc
        for e in facts.world.anomalies
        if e.statement_line_id in facts.line_by_id
    }
    units: dict[tuple[str, str], int] = {}
    for line in facts.world.statement_lines:
        if line.isrc and line.isrc not in anomaly_isrcs:
            units[(line.isrc, line.period)] = units.get((line.isrc, line.period), 0) + line.units
    unit_candidates = sorted((isrc, period) for (isrc, period), n in units.items() if n >= 5000)
    for isrc, period in [
        unit_candidates[int(i)] for i in gen.rng.permutation(len(unit_candidates))[:2]
    ]:
        track = facts.track_by_isrc[isrc]
        gen.add(
            category="sql_analytics",
            agent="analyst",
            tiers=["t1", "t2"],
            prompt=(
                f"Summing every statement line dated {period} across all feeds, how "
                f'many units did the track "{track.title}" (ISRC {isrc}) report?'
            ),
            answer_kind="count",
            expected=units[(isrc, period)],
            t2_checks=["sql_clean", "no_truth_access"],
            meta={
                "isrc": isrc,
                "period": period,
                "reference_sql": (
                    "SELECT COALESCE(sum(units), 0) AS units FROM label.statement_lines "
                    f"WHERE isrc = '{isrc}' AND period = '{period}'"
                ),
            },
        )

    # Top territory for a store in a period (unique maximum asserted). Aggregates run
    # over the dirty lines exactly as SQL over label.statement_lines would — the DB is
    # the reported world, corruption included.
    def top_unique(grouping: dict[str, int]) -> str | None:
        ranked = sorted(grouping.items(), key=lambda kv: (-kv[1], kv[0]))
        if len(ranked) >= 2 and ranked[0][1] == ranked[1][1]:
            return None
        return ranked[0][0] if ranked else None

    stores = sorted({s.name for s in facts.config.stores if s.revenue_type == "streaming"})
    emitted_top = 0
    for store in stores:
        if emitted_top >= 1:
            break
        period = gen.pick_period()
        grouping: dict[str, int] = {}
        for line in facts.world.statement_lines:
            if line.store == store and line.period == period:
                grouping[line.territory] = grouping.get(line.territory, 0) + line.units
        top = top_unique(grouping)
        if top is None:
            continue
        gen.add(
            category="sql_analytics",
            agent="analyst",
            tiers=["t1", "t2"],
            prompt=(
                f"Which territory reported the most units on the {store} store in "
                f"{period} (statement lines dated {period})?"
            ),
            answer_kind="value",
            expected=top,
            t2_checks=["sql_clean", "no_truth_access"],
            meta={
                "store": store,
                "period": period,
                "reference_sql": (
                    "SELECT territory, sum(units) AS units FROM label.statement_lines "
                    f"WHERE store = '{store}' AND period = '{period}' "
                    "GROUP BY territory ORDER BY units DESC LIMIT 1"
                ),
            },
        )
        emitted_top += 1
    if emitted_top < 1:
        raise GenerationError("sql_analytics: no unique-top territory found")

    # Top store by units in a period (again over the dirty lines, as SQL would see them).
    period = gen.pick_period()
    store_units: dict[str, int] = {}
    for line in facts.world.statement_lines:
        if line.period == period:
            store_units[line.store] = store_units.get(line.store, 0) + line.units
    top_store = top_unique(store_units)
    if top_store is None:
        raise GenerationError("sql_analytics: tied top store")
    gen.add(
        category="sql_analytics",
        agent="analyst",
        tiers=["t1", "t2"],
        prompt=f"Which store reported the highest total units across {period}?",
        answer_kind="value",
        expected=top_store,
        t2_checks=["sql_clean", "no_truth_access"],
        meta={
            "period": period,
            "reference_sql": (
                "SELECT store, sum(units) AS units FROM label.statement_lines "
                f"WHERE period = '{period}' GROUP BY store ORDER BY units DESC LIMIT 1"
            ),
        },
    )

    # One money aggregate on a single-currency statement.
    money_statement = None
    for statement in [clean_statements[int(i)] for i in gen.rng.permutation(len(clean_statements))]:
        lines = facts.lines_of_statement[statement.id]
        currencies = {line.currency for line in lines}
        if len(currencies) == 1:
            money_statement = (statement, next(iter(currencies)), lines)
            break
    if money_statement is None:
        raise GenerationError("sql_analytics: no single-currency statement")
    statement, currency, lines = money_statement
    total = sum((line.gross_amount for line in lines), Decimal("0"))
    gen.add(
        category="sql_analytics",
        agent="analyst",
        tiers=["t1", "t2"],
        prompt=(
            f"What is the total gross amount, in {currency}, of the "
            f"{facts.distributor_name[statement.distributor_id]} statement for "
            f"{statement.period} (statement id {statement.id})?"
        ),
        answer_kind="money",
        expected=str(total),
        tolerance="0.01",
        t2_checks=["sql_clean", "no_truth_access"],
        meta={
            "statement_id": statement.id,
            "currency": currency,
            "reference_sql": (
                "SELECT sum(gross_amount) AS total FROM label.statement_lines "
                f"WHERE statement_id = {statement.id}"
            ),
        },
    )

    # Statements received from one distributor across the window.
    dist = facts.world.distributors[int(gen.rng.integers(0, len(facts.world.distributors)))]
    n_statements = sum(1 for s in facts.world.statements if s.distributor_id == dist.id)
    gen.add(
        category="sql_analytics",
        agent="analyst",
        tiers=["t1", "t2"],
        prompt=(
            f"Across the whole seeded window ({facts.periods[0]} through "
            f"{facts.periods[-1]}), how many statements did we receive from "
            f"{dist.name}?"
        ),
        answer_kind="count",
        expected=n_statements,
        t2_checks=["sql_clean", "no_truth_access"],
        meta={
            "distributor_id": dist.id,
            "reference_sql": (
                "SELECT count(*) AS n FROM label.statements s "
                "JOIN label.distributors d ON d.id = s.distributor_id "
                f"WHERE d.name = '{_sql_quote(dist.name)}' "
                # The prompt scopes the seeded window explicitly; the SQL must too,
                # or a later `emit-period` month silently moves the answer.
                f"AND s.period BETWEEN '{facts.periods[0]}' AND '{facts.periods[-1]}'"
            ),
        },
    )


_RECON_T2 = ["used_scan", "no_batch", "sql_clean", "no_truth_access"]


def _gen_reconciliation(gen: _Gen) -> None:
    facts = gen.facts
    for period in facts.periods:
        flags = facts.expected_flags_by_period.get(period, [])
        borderline = facts.borderline_by_period.get(period, [])
        gen.add(
            category="reconciliation",
            agent="reconciler",
            tiers=["t1", "t2"],
            prompt=(
                f"Scan every statement for period {period} for reporting anomalies — "
                f"duplicates, unknown ISRCs, currency mismatches, negative units, "
                f"period bleed, suspicious territory spikes, and dashboard "
                f"divergence. Report only genuine, out-of-tolerance findings. Do not "
                f"submit a batch."
            ),
            answer_kind="flags",
            expected=_flags_expected(flags, borderline),
            t2_checks=list(_RECON_T2),
            meta={"period": period, "n_expected": len(flags)},
        )

    by_statement: dict[int, list[ExpectedFlag]] = {}
    for entry in facts.world.anomalies:
        if entry.expected_flag_kind is None:
            continue
        statement_id = facts.line_by_id[entry.statement_line_id].statement_id
        by_statement.setdefault(statement_id, []).append(
            ExpectedFlag(
                kind=entry.expected_flag_kind, source="label", line_id=entry.statement_line_id
            )
        )
    rich = sorted((s for s, f in by_statement.items() if len(f) >= 2))[:2]
    if len(rich) < 2:
        raise GenerationError("reconciliation: fewer than 2 statements with >= 2 anomalies")
    for statement_id in rich:
        statement = facts.statement_by_id[statement_id]
        gen.add(
            category="reconciliation",
            agent="reconciler",
            tiers=["t1", "t2"],
            prompt=(
                f"Scan one statement only: statement id {statement_id} "
                f"({facts.distributor_name[statement.distributor_id]}, period "
                f"{statement.period}). Report every out-of-tolerance anomaly on it. "
                f"Do not submit a batch."
            ),
            answer_kind="flags",
            expected=_flags_expected(
                sorted(by_statement[statement_id], key=lambda f: (f.kind, f.line_id)), []
            ),
            t2_checks=list(_RECON_T2),
            meta={"statement_id": statement_id, "period": statement.period},
        )


_MULTI_RECON_T2 = ["single_batch", "used_scan", "used_allocations", "sql_clean", "no_truth_access"]


def _gen_multi_step(gen: _Gen) -> None:
    facts = gen.facts
    thresholds = [Decimal(500), Decimal(1500), Decimal(2000), Decimal(750), Decimal(1250)]
    emitted = 0
    for period in facts.periods:
        if emitted >= 5:
            break
        if period == "2025-07":  # reserved for hand-multi_step-01
            continue
        threshold = thresholds[emitted]
        names = [facts.stage_name(a) for a in facts.artists_paid_over(period, threshold)]
        if not (4 <= len(names) <= 14 and facts.boundary_safe(period, threshold)):
            continue
        top = facts.artists_paid_over(period, threshold)[0]
        gen.add(
            category="multi_step",
            agent="reconciler",
            tiers=["t1", "t2", "t3"],
            prompt=(
                f"Run reconciliation for {period}: scan the period's statements for "
                f"anomalies, compute proposed allocations with corrupt reporting "
                f"excluded, and submit exactly one batch for human review with your "
                f"flags attached. Then answer: which artists should be paid more "
                f"than ${threshold:,} for {period}?"
            ),
            answer_kind="set",
            expected=sorted(names),
            t2_checks=list(_MULTI_RECON_T2),
            meta={
                "period": period,
                "threshold": str(threshold),
                "sample_allocation": {
                    "artist_id": top,
                    "net_payable": str(facts.payable(top, period).net_payable),
                },
            },
        )
        emitted += 1
    if emitted < 5:
        raise GenerationError(f"multi_step: only {emitted}/5 reconcile questions")

    amounts = [Decimal(10000), Decimal(40000), Decimal(15000), Decimal(30000), Decimal(20000)]
    combos = [
        ("streaming", "US"),
        ("streaming", "DE"),
        ("download", "US"),
        ("streaming", "GB"),
        ("download", "GB"),
    ]
    territory_names = {
        "US": "the United States (US)",
        "DE": "Germany (DE)",
        "GB": "the United Kingdom (GB)",
    }
    month = "2026-02"
    as_of = period_end_date(month)
    pool = facts.money_question_artists
    order = [pool[int(i)] for i in gen.rng.permutation(len(pool))]
    used = gen.used_artists.setdefault("multi_step", set())
    emitted = 0
    cursor = 0
    while emitted < 5 and cursor < len(order):
        artist_id = order[cursor]
        cursor += 1
        if artist_id in used:
            continue
        base, terms = facts.governing_terms(artist_id, as_of)
        if terms.escalators:
            continue
        revenue_type, territory = combos[emitted]
        rate = base_rate(terms, revenue_type, territory)
        if rate <= 0:
            continue
        amount = amounts[emitted]
        expected = money6(amount * rate)
        amendments = facts.royalties_amendments(artist_id, as_of)
        gold_id = amendments[-1].id if amendments else base.id
        gold_clause = "§A1" if amendments else "§3"
        phrase = "streaming revenue" if revenue_type == "streaming" else "download revenue"
        gen.add(
            category="multi_step",
            agent="counsel",
            tiers=["t1", "t2", "t3"],
            prompt=(
                f"Suppose {facts.stage_name(artist_id)} earns ${amount:,} in {phrase} "
                f"in {territory_names[territory]} during {month}. Under the terms "
                f"governing as of {as_of.isoformat()}, what royalty would that revenue "
                f"generate, before recoupment? Cite the rate clause you applied."
            ),
            answer_kind="money",
            expected=str(expected),
            tolerance="0.01",
            t2_checks=["money_via_calculator", "cites_clause"],
            meta={
                "artist_id": artist_id,
                "as_of": as_of.isoformat(),
                "revenue_type": revenue_type,
                "territory": territory,
                "amount": str(amount),
                "rate": str(rate),
                "gold_contract_id": gold_id,
                "gold_clause_no": gold_clause,
            },
        )
        used.add(artist_id)
        emitted += 1
    if emitted < 5:
        raise GenerationError(f"multi_step: only {emitted}/5 spot questions")


def _gen_abstention(gen: _Gen) -> None:
    facts = gen.facts
    roster = {a.stage_name.casefold() for a in facts.world.artists} | {
        a.legal_name.casefold() for a in facts.world.artists
    }
    fakes = [name for name in _FAKE_ARTISTS if name.casefold() not in roster]
    if len(fakes) < 7:
        raise GenerationError("fake-artist pool collides with the roster")

    counsel_forms = [
        ("What streaming royalty rate does {name} earn under their current deal?", "percent"),
        ("What is {name}'s net payable for 2026-03, after recoupment?", "money"),
        ("Does {name}'s agreement include a minimum guarantee per accounting period?", "bool"),
        ("Which territories are excluded from {name}'s deal?", "value"),
        ("How much unrecouped balance remains on {name}'s account as of 2026-06?", "money"),
    ]
    for fake, (template, masquerade) in zip(fakes[:5], counsel_forms, strict=True):
        gen.add(
            category="abstention",
            agent="counsel",
            tiers=["t1"],
            prompt=template.replace("{name}", fake),
            answer_kind="abstain",
            expected="ABSTAIN",
            meta={"fake_artist": fake},
            suffix_kind=masquerade,
        )

    analyst_forms = [
        "How many tracks does {name} have in our catalog as primary artist?",
        "How many releases carry {name}'s tracks?",
    ]
    for fake, template in zip(fakes[5:7], analyst_forms, strict=True):
        gen.add(
            category="abstention",
            agent="analyst",
            tiers=["t1"],
            prompt=template.replace("{name}", fake),
            answer_kind="abstain",
            expected="ABSTAIN",
            meta={"fake_artist": fake},
            suffix_kind="count",
        )

    contract_ids = {c.id for c in facts.world.contracts}
    fake_id = 99901
    while fake_id in contract_ids:
        fake_id += 1
    gen.add(
        category="abstention",
        agent="counsel",
        tiers=["t1"],
        prompt=(
            f"What royalty rates does contract FBR-C-{fake_id:05d} set in its §3? "
            f"I need the streaming figure."
        ),
        answer_kind="abstain",
        expected="ABSTAIN",
        meta={"fake_contract_id": fake_id},
        suffix_kind="percent",
    )


_GENERATORS: dict[Category, Callable[[_Gen], None]] = {
    "catalog_lookup": _gen_catalog_lookup,
    "contract_terms": _gen_contract_terms,
    "royalty_math": _gen_royalty_math,
    "recoupment_state": _gen_recoupment_state,
    "cross_collateral": _gen_cross_collateral,
    "sql_analytics": _gen_sql_analytics,
    "reconciliation": _gen_reconciliation,
    "multi_step": _gen_multi_step,
    "abstention": _gen_abstention,
}


# Smoke picks: (category, predicate on meta/shape) — the first generated question of
# each scriptable shape; the adversarial slot is the hand canary-read case.
def _mark_subsets(questions: list[Question]) -> list[Question]:
    by_category: dict[str, list[Question]] = {}
    for question in questions:
        by_category.setdefault(question.category, []).append(question)

    in_gate: set[str] = {q.id for q in questions if q.source == "hand"}
    for category, quota in _GATE_GENERATED.items():
        generated = [q for q in by_category.get(category, []) if q.source == "generated"]
        in_gate.update(q.id for q in generated[:quota])

    smoke_picks: set[str] = {"hand-adversarial-01"}
    smoke_rules: list[tuple[Category, Callable[[Question], bool]]] = [
        ("catalog_lookup", lambda q: "reference_sql" in q.meta and q.answer_kind == "count"),
        ("contract_terms", lambda q: q.meta.get("gold_clause_no") == "§3"),
        ("royalty_math", lambda q: q.meta.get("field") == "net_payable"),
        ("recoupment_state", lambda q: q.answer_kind == "bool"),
        ("cross_collateral", lambda q: q.meta.get("form") == "balance"),
        ("sql_analytics", lambda q: q.answer_kind == "count" and "statement_id" in q.meta),
        ("reconciliation", lambda q: "period" in q.meta and "statement_id" not in q.meta),
        ("multi_step", lambda q: q.agent == "reconciler"),
        ("abstention", lambda q: "fake_artist" in q.meta and q.agent == "counsel"),
    ]
    for category, predicate in smoke_rules:
        generated = [q for q in by_category.get(category, []) if q.source == "generated"]
        pick = next((q for q in generated if predicate(q)), None)
        if pick is None:
            raise GenerationError(f"smoke: no scriptable {category} question")
        smoke_picks.add(pick.id)

    return [
        q.model_copy(update={"in_gate": q.id in in_gate, "in_smoke": q.id in smoke_picks})
        for q in questions
    ]


def generate(seed: int) -> Suite:
    facts = build_facts(seed)
    rng = np.random.Generator(np.random.PCG64(np.random.SeedSequence([seed, _SUITE_STREAM])))
    gen = _Gen(facts=facts, rng=rng)

    for category in CATEGORIES:
        generator = _GENERATORS.get(category)
        if generator is not None:
            generator(gen)
    _resolve_hand_cases(gen)

    ordered: list[Question] = []
    by_category: dict[str, list[Question]] = {}
    for question in gen.questions:
        by_category.setdefault(question.category, []).append(question)
    for category in CATEGORIES:
        rows = by_category.get(category, [])
        rows.sort(key=lambda q: (q.source == "hand", q.id))
        target = CATEGORY_TARGETS[category]
        if len(rows) != target:
            raise GenerationError(f"{category}: {len(rows)} questions != target {target}")
        ordered.extend(rows)

    marked = _mark_subsets(ordered)
    return Suite(
        name="core",
        world_seed=seed,
        suite_hash=suite_hash(marked),
        questions=marked,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="evals generate", description="Generate evals/suites/core.json from the answer key."
    )
    parser.add_argument("--seed", type=int, default=None, help="world seed (default: settings)")
    parser.add_argument("--out", type=Path, default=SUITES_DIR / "core.json")
    parser.add_argument(
        "--check",
        action="store_true",
        help="regenerate and diff against the committed file; exit 1 on drift",
    )
    parser.add_argument(
        "--load-db",
        action="store_true",
        help="also upsert truth.qa_answer_key from the generated suite (needs DATABASE_URL)",
    )
    args = parser.parse_args(argv)

    from backline.config import get_settings

    seed = args.seed if args.seed is not None else get_settings().world_seed
    suite = generate(seed)
    serialized = dump_suite(suite)

    if args.check:
        committed = Path(args.out)
        if not committed.exists():
            print(f"missing {committed} — run `python -m evals generate`", file=sys.stderr)
            return 1
        if committed.read_text(encoding="utf-8") != serialized:
            print(
                f"suite drift: regenerating does not reproduce {committed} "
                f"(committed hash {load_suite(committed).suite_hash}, regenerated "
                f"{suite.suite_hash}) — regenerate and commit, and explain the change "
                f"in the PR",
                file=sys.stderr,
            )
            return 1
        print(f"suite OK: {committed} reproduces exactly (hash {suite.suite_hash})")
    else:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(serialized, encoding="utf-8")
        counts = ", ".join(f"{k}={v}" for k, v in suite.counts().items())
        print(f"wrote {args.out} — {len(suite.questions)} questions (hash {suite.suite_hash})")
        print(f"  {counts}")
        print(
            f"  gate subset: {sum(1 for q in suite.questions if q.in_gate)} · "
            f"smoke subset: {sum(1 for q in suite.questions if q.in_smoke)}"
        )

    if args.load_db:
        import asyncpg

        async def _load() -> int:
            settings = get_settings()
            pool = await asyncpg.create_pool(settings.database_url, min_size=1, max_size=2)
            try:
                return await load_answer_key(pool, suite)
            finally:
                await pool.close()

        n = asyncio.run(_load())
        print(f"loaded {n} rows into truth.qa_answer_key")
    return 0


if __name__ == "__main__":
    sys.exit(main())
