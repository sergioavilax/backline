"""In-memory world entities — exactly the rows that land in Postgres (§3.3).

``build_world`` (datagen.world) produces one ``World`` deterministically from
``(config, seed)``; the DB loader, feed writers, PDF renderer, truth engine, and
fingerprint all consume this object. Ids are explicit and stable — no sequences.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any


@dataclass(frozen=True)
class Artist:
    id: int
    stage_name: str
    legal_name: str
    joined_at: date
    country: str


@dataclass(frozen=True)
class Release:
    id: int
    upc: str
    title: str
    imprint: str
    release_date: date
    primary_artist_id: int | None  # None for label compilations (not a DB column)
    is_compilation: bool
    physical: bool  # sold as physical units by the northstar feed


@dataclass(frozen=True)
class Track:
    id: int
    isrc: str
    title: str
    primary_artist_id: int
    duration_s: int
    origin_release_id: int
    origin_release_date: date


@dataclass(frozen=True)
class ReleaseTrack:
    release_id: int
    track_id: int
    position: int


@dataclass(frozen=True)
class Contract:
    id: int
    artist_id: int
    doc_path: str
    effective_from: date
    effective_to: date | None
    kind: str  # base | amendment
    terms_json: dict[str, Any]  # canonical royaltycalc terms doc
    supersedes_contract_id: int | None  # amendments only
    replaced_sections: tuple[str, ...]  # amendments only
    has_canary: bool  # rendered into the PDF only (§4.6)


@dataclass(frozen=True)
class RecoupAccount:
    artist_id: int
    xcollat_group_id: str
    opening_balance: Decimal


@dataclass(frozen=True)
class Advance:
    id: int
    artist_id: int
    contract_id: int
    amount: Decimal
    currency: str
    granted_at: date


@dataclass(frozen=True)
class Expense:
    id: int
    artist_id: int
    expense_class: str
    amount: Decimal
    currency: str
    incurred_at: date
    recoupable: bool


@dataclass(frozen=True)
class Distributor:
    id: int
    name: str
    dialect: str
    feed_key: str


@dataclass(frozen=True)
class Statement:
    id: int
    distributor_id: int
    period: str
    received_at: date
    raw_path: str
    status: str  # received | ingested


@dataclass(frozen=True)
class StatementLine:
    id: int
    statement_id: int
    period: str  # the line's own period — differs from the statement's for period_bleed
    isrc: str  # "" for physical (release-level) lines
    upc: str | None
    store: str
    territory: str
    units: int
    gross_amount: Decimal  # native feed currency, 6dp
    currency: str
    line_hash: str


@dataclass(frozen=True)
class DashboardStream:
    period: str
    isrc: str
    store: str
    streams: int


@dataclass(frozen=True)
class AnomalyEntry:
    id: int
    kind: str
    statement_line_id: int
    expected_flag_kind: str | None  # None = borderline; correct behavior is NOT flagging
    note: str


@dataclass(frozen=True)
class LedgerRow:
    artist_id: int
    period: str
    gross: Decimal
    recouped: Decimal
    net_payable: Decimal
    balance_after: Decimal


@dataclass
class World:
    seed: int
    artists: list[Artist] = field(default_factory=list)
    releases: list[Release] = field(default_factory=list)
    tracks: list[Track] = field(default_factory=list)
    release_tracks: list[ReleaseTrack] = field(default_factory=list)
    contracts: list[Contract] = field(default_factory=list)
    recoup_accounts: list[RecoupAccount] = field(default_factory=list)
    advances: list[Advance] = field(default_factory=list)
    expenses: list[Expense] = field(default_factory=list)
    distributors: list[Distributor] = field(default_factory=list)
    statements: list[Statement] = field(default_factory=list)
    statement_lines: list[StatementLine] = field(default_factory=list)  # dirty (as reported)
    clean_lines: list[StatementLine] = field(default_factory=list)  # truth input, never in DB
    dashboard_streams: list[DashboardStream] = field(default_factory=list)  # dirty
    anomalies: list[AnomalyEntry] = field(default_factory=list)
    ledger: list[LedgerRow] = field(default_factory=list)

    def track_by_isrc(self) -> dict[str, Track]:
        return {t.isrc: t for t in self.tracks}

    def release_by_upc(self) -> dict[str, Release]:
        return {r.upc: r for r in self.releases}

    def base_contracts_of(self, artist_id: int) -> list[Contract]:
        return [c for c in self.contracts if c.artist_id == artist_id and c.kind == "base"]

    def amendments_of(self, base_contract_id: int) -> list[Contract]:
        return [
            c
            for c in self.contracts
            if c.kind == "amendment" and c.supersedes_contract_id == base_contract_id
        ]
