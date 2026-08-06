"""Answer-key access for the suite generator: one in-memory world, indexed.

``evals/generate_suite.py`` derives questions from the *same* deterministic world the
DB is seeded from — ``datagen.assemble.build_world(config, WORLD_SEED)`` — so the
generator needs no database and every expected value comes from the answer key
(``world.ledger`` == ``truth.expected_ledger``, ``world.anomalies`` ==
``truth.anomaly_registry``) or from the world rows the DB carries verbatim
(ids are explicit and stable; the loader COPYs them unchanged).

Taint rule: an artist whose *statement lines* are corrupted by a registered,
non-borderline anomaly (any kind except ``dashboard_gap``, which corrupts the
dashboard side — D-005) cannot anchor a money question: their DB-computed ledger
legitimately diverges from truth until the Reconciler excludes the corruption.
Money questions target untainted artists; tainted ones are reconciliation material.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from functools import cached_property

from backline.royaltycalc import Terms, resolve_terms
from datagen.assemble import BuiltWorld, build_world
from datagen.config import WorldConfig, load_world_config, period_end_date
from datagen.truthengine import TruthEngine
from datagen.worldmodel import (
    AnomalyEntry,
    Artist,
    Contract,
    LedgerRow,
    Release,
    Statement,
    StatementLine,
    Track,
    World,
)

__all__ = ["ExpectedFlag", "WorldFacts", "build_facts"]


@dataclass(frozen=True)
class ExpectedFlag:
    kind: str
    source: str  # seeded periods live in label.statement_lines
    line_id: int


@dataclass
class WorldFacts:
    built: BuiltWorld
    engine: TruthEngine = field(init=False)

    def __post_init__(self) -> None:
        self.engine = TruthEngine(self.built.structure)

    # ── plumbing ─────────────────────────────────────────────────────────────

    @property
    def config(self) -> WorldConfig:
        return self.built.structure.config

    @property
    def world(self) -> World:
        return self.built.world

    @property
    def periods(self) -> tuple[str, ...]:
        return self.config.periods

    @cached_property
    def artist_by_id(self) -> dict[int, Artist]:
        return {a.id: a for a in self.world.artists}

    def stage_name(self, artist_id: int) -> str:
        return self.artist_by_id[artist_id].stage_name

    @cached_property
    def track_by_isrc(self) -> dict[str, Track]:
        return self.world.track_by_isrc()

    @cached_property
    def release_by_upc(self) -> dict[str, Release]:
        return self.world.release_by_upc()

    @cached_property
    def line_by_id(self) -> dict[int, StatementLine]:
        return {line.id: line for line in self.world.statement_lines}

    @cached_property
    def statement_by_id(self) -> dict[int, Statement]:
        return {s.id: s for s in self.world.statements}

    @cached_property
    def lines_of_statement(self) -> dict[int, list[StatementLine]]:
        grouped: dict[int, list[StatementLine]] = defaultdict(list)
        for line in self.world.statement_lines:
            grouped[line.statement_id].append(line)
        return dict(grouped)

    @cached_property
    def distributor_name(self) -> dict[int, str]:
        return {d.id: d.name for d in self.world.distributors}

    @cached_property
    def ledger(self) -> dict[tuple[int, str], LedgerRow]:
        return {(row.artist_id, row.period): row for row in self.world.ledger}

    # ── anomaly-derived facts ────────────────────────────────────────────────

    def _attribute_artist(self, line: StatementLine) -> int | None:
        attribution = self.engine.attribute(line)
        return attribution[0] if attribution is not None else None

    @cached_property
    def tainted_artists(self) -> frozenset[int]:
        """Artists whose reported lines carry registered (non-borderline) corruption."""
        tainted: set[int] = set()
        for entry in self.world.anomalies:
            if entry.expected_flag_kind is None or entry.kind == "dashboard_gap":
                continue
            line = self.line_by_id.get(entry.statement_line_id)
            if line is None:
                continue
            artist = self._attribute_artist(line)
            if artist is not None:
                tainted.add(artist)
        return frozenset(tainted)

    def anomaly_statement_period(self, entry: AnomalyEntry) -> str:
        """Anomalies group by *statement* period (period_bleed lines carry their own)."""
        line = self.line_by_id[entry.statement_line_id]
        return self.statement_by_id[line.statement_id].period

    @cached_property
    def expected_flags_by_period(self) -> dict[str, list[ExpectedFlag]]:
        expected: dict[str, list[ExpectedFlag]] = defaultdict(list)
        for entry in self.world.anomalies:
            if entry.expected_flag_kind is None:
                continue
            expected[self.anomaly_statement_period(entry)].append(
                ExpectedFlag(
                    kind=entry.expected_flag_kind,
                    source="label",
                    line_id=entry.statement_line_id,
                )
            )
        for flags in expected.values():
            flags.sort(key=lambda f: (f.kind, f.line_id))
        return dict(expected)

    @cached_property
    def borderline_by_period(self) -> dict[str, list[int]]:
        borderline: dict[str, list[int]] = defaultdict(list)
        for entry in self.world.anomalies:
            if entry.expected_flag_kind is None:
                borderline[self.anomaly_statement_period(entry)].append(entry.statement_line_id)
        return dict(borderline)

    def expected_flags_by_statement(self, statement_id: int) -> list[ExpectedFlag]:
        return sorted(
            (
                ExpectedFlag(
                    kind=entry.expected_flag_kind or "",
                    source="label",
                    line_id=entry.statement_line_id,
                )
                for entry in self.world.anomalies
                if entry.expected_flag_kind is not None
                and self.line_by_id[entry.statement_line_id].statement_id == statement_id
            ),
            key=lambda f: (f.kind, f.line_id),
        )

    # ── money facts (the answer key proper) ──────────────────────────────────

    def payable(self, artist_id: int, period: str) -> LedgerRow:
        return self.ledger[(artist_id, period)]

    @cached_property
    def money_question_artists(self) -> list[int]:
        """Untainted artists, sorted — the only valid anchors for T1 money questions."""
        return sorted(a.id for a in self.world.artists if a.id not in self.tainted_artists)

    def artists_paid_over(self, period: str, threshold: Decimal) -> list[int]:
        return sorted(
            row.artist_id
            for row in self.world.ledger
            if row.period == period and row.net_payable > threshold
        )

    @cached_property
    def _max_fx(self) -> Decimal:
        rates = [rate for table in self.config.fx_rates.values() for rate in table.values()]
        return max([*rates, Decimal(1)])

    @cached_property
    def _taint_shift_events(self) -> dict[int, list[tuple[str, Decimal]]]:
        """Per tainted artist: (statement period, USD upper bound on the payable shift
        that one corrupted line can cause under any exclude/keep handling)."""
        ceiling_rate = Decimal("0.65")  # above every seeded rate + escalator bump
        events: dict[int, list[tuple[str, Decimal]]] = defaultdict(list)
        for entry in self.world.anomalies:
            if entry.expected_flag_kind is None or entry.kind == "dashboard_gap":
                continue
            line = self.line_by_id.get(entry.statement_line_id)
            if line is None:
                continue
            artist = self._attribute_artist(line)
            if artist is None:
                continue
            period = self.statement_by_id[line.statement_id].period
            bound = abs(line.gross_amount) * self._max_fx * ceiling_rate
            events[artist].append((period, bound))
        return dict(events)

    def _taint_shift(self, artist_id: int, period: str) -> Decimal:
        """Cumulative shift bound through ``period`` (recoupment carries earlier-period
        corruption forward, so all corrupted lines up to the period count)."""
        return sum(
            (bound for p, bound in self._taint_shift_events.get(artist_id, []) if p <= period),
            Decimal(0),
        )

    def boundary_safe(self, period: str, threshold: Decimal) -> bool:
        """Corruption handling (exclude vs keep) must not be able to flip any artist's
        membership in the pay-over-threshold set: every tainted artist's payable must
        clear the threshold by more than their worst-case corruption shift."""
        for row in self.world.ledger:
            if row.period != period or row.artist_id not in self.tainted_artists:
                continue
            shift = self._taint_shift(row.artist_id, period)
            if row.net_payable - shift <= threshold < row.net_payable + shift:
                return False
        return True

    # ── deals & rates ────────────────────────────────────────────────────────

    def era_base(self, artist_id: int, as_of: date) -> Contract:
        return self.built.structure.era_contract_for(artist_id, as_of)

    def governing_terms(self, artist_id: int, as_of: date) -> tuple[Contract, Terms]:
        """The era base contract governing ``as_of`` and its amendment-resolved terms."""
        base = self.era_base(artist_id, as_of)
        terms = resolve_terms(
            self.engine.parsed_docs[base.id],
            self.engine.amendments_of.get(base.id, []),
            as_of=as_of,
        )
        return base, terms

    def base_only_terms(self, base_contract_id: int, as_of: date) -> Terms:
        """The base contract's own terms with no amendments applied (history questions)."""
        return resolve_terms(self.engine.parsed_docs[base_contract_id], [], as_of=as_of)

    def royalties_amendments(self, artist_id: int, as_of: date) -> list[Contract]:
        """Effective amendments (as of) that replace the base's royalties section."""
        base = self.era_base(artist_id, as_of)
        return sorted(
            (
                c
                for c in self.world.amendments_of(base.id)
                if c.effective_from <= as_of and "royalties" in c.replaced_sections
            ),
            key=lambda c: (c.effective_from, c.id),
        )

    def window_end(self) -> date:
        return period_end_date(self.periods[-1])


def build_facts(seed: int) -> WorldFacts:
    return WorldFacts(built=build_world(load_world_config(), seed))
