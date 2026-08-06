"""Request/response models for the Phase 6 API surface.

Everything monetary is ``Decimal`` end to end (invariant 1) — Pydantic v2 serializes
``Decimal`` as a JSON *string*, so no float ever leaves this API for a monetary value.
JSONB payloads (span attrs, batch summaries, message content) pass through as dicts;
their money fields were stringified at write time by ``canonical_dumps``.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

# ── meta ─────────────────────────────────────────────────────────────────────


class MetaOut(BaseModel):
    """What the UI needs to label itself honestly (provider mode, model policy)."""

    version: str
    demo_mode: bool
    providers: list[str]
    planner_model: str
    utility_model: str
    router_model: str
    world_seed: int


# ── sessions & messages ──────────────────────────────────────────────────────


class SessionCreate(BaseModel):
    title: str | None = Field(default=None, max_length=200)


class SessionOut(BaseModel):
    id: UUID
    title: str | None
    created_at: datetime
    n_messages: int = 0
    last_message_at: datetime | None = None


class MessageOut(BaseModel):
    """One chat turn. ``content`` is the persisted JSONB: ``{"text": ...}`` for user
    turns; assistant turns add agent/run/route/citation metadata (see chat.py)."""

    id: UUID
    session_id: UUID
    role: str
    content: dict[str, Any]
    created_at: datetime


class SessionDetailOut(BaseModel):
    session: SessionOut
    messages: list[MessageOut]


class MessageIn(BaseModel):
    text: str = Field(min_length=1, max_length=8000)
    # Pin a specific agent, skipping the router ("ask Counsel directly").
    agent: Literal["counsel", "analyst", "reconciler"] | None = None


# ── runs & spans (Trace Inspector) ───────────────────────────────────────────


class RunOut(BaseModel):
    id: UUID
    session_id: UUID | None
    agent: str
    status: str
    started_at: datetime
    finished_at: datetime | None
    cost_usd: Decimal
    meta: dict[str, Any]


class SpanOut(BaseModel):
    id: UUID
    run_id: UUID
    parent_id: UUID | None
    kind: str
    name: str
    started_at: datetime
    ended_at: datetime | None
    attrs: dict[str, Any]


class RunDetailOut(BaseModel):
    run: RunOut
    spans: list[SpanOut]


class RunListOut(BaseModel):
    runs: list[RunOut]
    total: int


# ── review queue ─────────────────────────────────────────────────────────────


class BatchOut(BaseModel):
    id: int
    period: str
    status: Literal["proposed", "approved", "rejected"]
    submitted_by_run: UUID | None
    summary: dict[str, Any]
    created_at: datetime
    n_allocations: int = 0
    n_flags: int = 0
    total_net_payable: Decimal = Decimal("0")


class AllocationOut(BaseModel):
    artist_id: int
    stage_name: str | None
    period: str
    net_payable: Decimal
    line_detail: dict[str, Any]


class FlagOut(BaseModel):
    id: int
    kind: str
    severity: str
    payload: dict[str, Any]
    evidence: list[dict[str, Any]] = Field(default_factory=list)


class PromotionPreviewOut(BaseModel):
    """The diff-style "what changes if approved" panel."""

    statements_to_promote: list[dict[str, Any]]
    n_staged_lines: int
    staged_gross_by_currency: dict[str, Decimal]
    allocation_total: Decimal
    n_paid_artists: int


class BatchDetailOut(BaseModel):
    batch: BatchOut
    allocations: list[AllocationOut]
    flags: list[FlagOut]
    promotion: PromotionPreviewOut


class ReviewActionIn(BaseModel):
    note: str = Field(default="", max_length=2000)


class RejectIn(BaseModel):
    note: str = Field(min_length=1, max_length=2000, description="rejection requires a note")


# ── evals ────────────────────────────────────────────────────────────────────


class EvalRunOut(BaseModel):
    id: UUID
    suite_hash: str
    model: str
    git_sha: str | None
    started_at: datetime
    finished_at: datetime | None
    summary: dict[str, Any]


class EvalResultOut(BaseModel):
    question_id: str
    tier: str
    score: Decimal | None
    passed: bool | None
    detail: dict[str, Any]


class EvalRunDetailOut(BaseModel):
    run: EvalRunOut
    results: list[EvalResultOut]


class EvalListOut(BaseModel):
    runs: list[EvalRunOut]


class BaselineOut(BaseModel):
    """The committed regression baseline (evals/results/baseline.json), verbatim."""

    baselines: list[dict[str, Any]]


# ── catalog browse ───────────────────────────────────────────────────────────


class ArtistOut(BaseModel):
    id: int
    stage_name: str
    legal_name: str
    joined_at: date
    n_tracks: int = 0
    n_releases: int = 0
    n_contracts: int = 0


class ArtistListOut(BaseModel):
    artists: list[ArtistOut]
    total: int


class ContractOut(BaseModel):
    id: int
    code: str  # FBR-C-00501 / FBR-A-00612 — the citation-facing identifier
    kind: str
    effective_from: date
    effective_to: date | None
    doc_path: str


class ReleaseOut(BaseModel):
    id: int
    upc: str
    title: str
    imprint: str
    release_date: date
    n_tracks: int = 0


class ReleaseListOut(BaseModel):
    releases: list[ReleaseOut]
    total: int


class TrackOut(BaseModel):
    id: int
    isrc: str
    title: str
    duration_s: int
    primary_artist_id: int
    stage_name: str | None = None


class TrackListOut(BaseModel):
    tracks: list[TrackOut]
    total: int


class ArtistDetailOut(BaseModel):
    artist: ArtistOut
    releases: list[ReleaseOut]
    contracts: list[ContractOut]


class ClauseOut(BaseModel):
    """One contract clause, resolved from a structural citation (FBR-C-00501 §3)."""

    code: str
    contract_id: int
    clause_no: str
    heading: str | None
    text: str
    artist_id: int | None
    stage_name: str | None
    kind: str
    effective_from: date | None
    effective_to: date | None
