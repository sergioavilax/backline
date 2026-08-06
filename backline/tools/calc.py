"""``calc_royalties`` — the calculator tool (§4.3). **All money arithmetic goes here.**

Thin over ``tools.ledger`` (which is thin over ``royaltycalc`` — D-001). Two modes in
one tool, so the model never does mental math for either question shape:

- **ledger** (``period`` set): the artist's real statement history through that period —
  full recoupment chain, minimum guarantees, cross-collateral pooling. ``exclude_line_ids``
  drops statement lines the agent has flagged as anomalous; ``include_staged`` adds lines
  it staged via ``ingest_statement`` (the fresh-drop workflow).
- **spot** (``rows`` set): hypothetical revenue under the terms governing ``as_of_date``,
  with real escalator state. Pre-recoupment, and the output says so.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from backline.core.runtime import Tool
from backline.tools.artists import resolve_artist
from backline.tools.context import ToolContext
from backline.tools.ledger import compute_ledger_slice, compute_spot_quote

_CODE = {"base": "FBR-C", "amendment": "FBR-A"}


class RevenueRow(BaseModel):
    model_config = ConfigDict(frozen=True)

    revenue_type: Literal["streaming", "download", "physical", "sync"]
    territory: str = Field(pattern=r"^([A-Z]{2}|WW)$", description="ISO-2 country, or WW")
    amount: Decimal = Field(ge=0, description="revenue amount in `currency` (label net receipts)")
    currency: Literal["USD", "EUR", "GBP", "JPY"] = "USD"


class CalcRoyaltiesParams(BaseModel):
    model_config = ConfigDict(frozen=True)

    artist: str | None = Field(default=None, description="artist stage or legal name")
    artist_id: int | None = None
    period: str | None = Field(
        default=None,
        pattern=r"^\d{4}-\d{2}$",
        description="ledger mode: compute the artist's real statements through this month",
    )
    rows: list[RevenueRow] | None = Field(
        default=None,
        description="spot mode: hypothetical revenue rows to rate under governing terms",
    )
    as_of_date: date | None = Field(
        default=None, description="spot mode: resolve terms as of this date (default: latest)"
    )
    exclude_line_ids: list[int] = Field(
        default_factory=list,
        description="ledger mode: label statement line ids to exclude (suspected anomalies)",
    )
    exclude_staged_line_ids: list[int] = Field(
        default_factory=list,
        description="ledger mode: staged (staging.ingested_lines) ids to exclude — staged "
        "ids are a separate sequence from label line ids",
    )
    include_staged: bool = Field(
        default=False,
        description="ledger mode: also include lines staged via ingest_statement",
    )

    @model_validator(mode="after")
    def _check_mode(self) -> CalcRoyaltiesParams:
        if self.artist is None and self.artist_id is None:
            raise ValueError("provide artist (name) or artist_id")
        if (self.period is None) == (self.rows is None):
            raise ValueError("provide exactly one of `period` (ledger mode) or `rows` (spot mode)")
        if self.rows is not None and not self.rows:
            raise ValueError("rows must be non-empty in spot mode")
        return self


def _pct(rate: Decimal) -> str:
    # :f forbids scientific notation ('0.1' * 100 normalizes to 1E+1 otherwise).
    return f"{(rate * 100).normalize():f}%"


def build_calc_royalties_tool(ctx: ToolContext) -> Tool[CalcRoyaltiesParams]:
    async def handler(params: CalcRoyaltiesParams) -> str:
        artist = await resolve_artist(ctx.pool, artist=params.artist, artist_id=params.artist_id)
        if params.period is not None:
            return await _ledger(params, artist.id, artist.stage_name)
        return await _spot(params, artist.id, artist.stage_name)

    async def _ledger(params: CalcRoyaltiesParams, artist_id: int, stage_name: str) -> str:
        s = await compute_ledger_slice(
            ctx.pool,
            artist_id=artist_id,
            period=params.period or "",
            exclude_line_ids=tuple(params.exclude_line_ids),
            exclude_staged_line_ids=tuple(params.exclude_staged_line_ids),
            include_staged=params.include_staged,
        )
        lines = [
            f"Royalty ledger — {stage_name} (artist {artist_id}), period {s.period}",
            f"Recoupment chain computed {s.start_period}..{s.period} via royaltycalc.",
            "",
            f"  gross royalties:  {s.gross} USD",
            f"  recouped:         {s.recouped}",
            f"  MG top-up:        {s.mg_topup}",
            f"  net payable:      {s.net_payable} USD  (rounded to cents, half-even)",
            f"  unrecouped balance after: {s.balance_after}",
            "",
            "Accounts:",
        ]
        lines.extend(
            f"  {a.account}: earnings {a.earnings} · charges {a.charges} · "
            f"recouped {a.recouped} · balance after {a.balance_after}"
            for a in s.outcome.accounts
        )
        if s.by_revenue_type:
            rollup = " · ".join(
                f"{revenue_type} {n} lines → {royalty}"
                for revenue_type, (n, royalty) in sorted(s.by_revenue_type.items())
            )
            lines.append(f"This period by revenue type: {rollup}")
        # Attribution always lands on era *base* contracts (amendments merge into them).
        contracts = ", ".join(f"FBR-C-{cid:05d}" for cid in s.contracts_used)
        if contracts:
            lines.append(f"Era contracts earning this period: {contracts}")
        lines.append(
            f"Lines: {s.n_lines_used} used across the chain"
            + (f" ({s.n_staged_used} staged)" if s.n_staged_used else "")
            + (
                f" · excluded on request: {list(s.excluded_line_ids)}"
                if s.excluded_line_ids
                else ""
            )
            + (
                f" · staged excluded on request: {list(s.excluded_staged_line_ids)}"
                if s.excluded_staged_line_ids
                else ""
            )
            + (
                f" · auto-excluded (negative/zero units or negative amount): "
                f"{list(s.auto_excluded_line_ids)}"
                if s.auto_excluded_line_ids
                else ""
            )
        )
        return "\n".join(lines)

    async def _spot(params: CalcRoyaltiesParams, artist_id: int, stage_name: str) -> str:
        assert params.rows is not None
        quote = await compute_spot_quote(
            ctx.pool,
            artist_id=artist_id,
            rows=[(r.revenue_type, r.territory, r.amount, r.currency) for r in params.rows],
            as_of=params.as_of_date,
        )
        out = [
            f"Spot royalty quote — {stage_name} (artist {artist_id}) as of {quote.as_of}",
            f"Governing base contract: FBR-C-{quote.contract_id:05d} (amendments applied "
            f"as of the date); FX at the {quote.fx_period} fixed rate.",
            f"Escalator state: cumulative gross {quote.cumulative_gross_usd} USD at period "
            f"start → active bump {_pct(quote.active_bump)}.",
            "",
        ]
        out.extend(
            f"  {line.revenue_type} {line.territory} {line.amount} {line.currency} → "
            f"{line.usd_amount} USD x rate {_pct(line.rate)} = {line.royalty} USD"
            for line in quote.lines
        )
        out.append("")
        out.append(
            f"Total gross royalty: {quote.total_royalty} USD — PRE-RECOUPMENT. Any "
            f"unrecouped balance recoups first; use ledger mode (period=...) for payable."
        )
        return "\n".join(out)

    return Tool(
        name="calc_royalties",
        description=(
            "Compute royalties with the label's one royalty engine — use this for ALL "
            "money arithmetic; never compute royalties yourself. Ledger mode "
            "(artist + period): the artist's real statement history through that month — "
            "rate application, FX, escalators, recoupment waterfall, minimum guarantees, "
            "cross-collateral pooling; optionally exclude flagged statement lines by id, "
            "or include staged lines from ingest_statement. Spot mode (artist + rows): "
            "rate hypothetical revenue rows under the terms governing as_of_date "
            "(pre-recoupment quote)."
        ),
        params=CalcRoyaltiesParams,
        handler=handler,
    )
