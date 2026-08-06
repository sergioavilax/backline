"""The truth engine: ``truth.expected_ledger`` for every artist x period.

Consumes the *clean* line set (anomalies are reporting corruptions — the clean world is
the payable truth, see D-003) and computes each artist-period through
``backline.royaltycalc`` — the same engine the runtime calculator tool will use (D-001).
Attribution rules owned here:

- digital lines attach by ISRC -> track -> primary artist; physical (blank-ISRC) lines by
  UPC -> release -> the release's artist;
- a recording pays under the era contract governing its *origin* release date (compilation
  appearances still pay under the original era's deal);
- terms resolve as of the period's last day (amendments apply from their effective date);
- advances charge their contract's account; recoupable expenses charge the era account
  at ``incurred_at``.
"""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

from backline.royaltycalc import (
    ArtistState,
    RevenueLine,
    Terms,
    TermsDoc,
    compute_artist_period,
    money6,
    parse_terms_doc,
    resolve_terms,
)
from datagen.config import period_end_date
from datagen.world import Structure
from datagen.worldmodel import LedgerRow, StatementLine

ZERO = Decimal("0")


class TruthEngine:
    def __init__(self, structure: Structure) -> None:
        self.structure = structure
        self.config = structure.config
        world = structure.world
        self.track_by_isrc = {t.isrc: t for t in world.tracks}
        self.release_by_upc = {r.upc: r for r in world.releases}
        self.revenue_type_of_store = {s.name: s.revenue_type for s in self.config.stores}
        self.parsed_docs: dict[int, TermsDoc] = {
            c.id: parse_terms_doc(c.terms_json) for c in world.contracts
        }
        self.amendments_of: dict[int, list[TermsDoc]] = defaultdict(list)
        for c in world.contracts:
            if c.kind == "amendment" and c.supersedes_contract_id is not None:
                self.amendments_of[c.supersedes_contract_id].append(self.parsed_docs[c.id])
        self._terms_cache: dict[tuple[int, str], Terms] = {}

    def governing_terms(self, base_contract_id: int, period: str) -> Terms:
        key = (base_contract_id, period)
        cached = self._terms_cache.get(key)
        if cached is None:
            cached = resolve_terms(
                self.parsed_docs[base_contract_id],
                self.amendments_of.get(base_contract_id, []),
                as_of=period_end_date(period),
            )
            self._terms_cache[key] = cached
        return cached

    def attribute(self, line: StatementLine) -> tuple[int, int] | None:
        """-> (artist_id, base_contract_id), or None if the line matches no catalog."""
        if line.isrc:
            track = self.track_by_isrc.get(line.isrc)
            if track is None:
                return None
            era = self.structure.era_contract_for(
                track.primary_artist_id, track.origin_release_date
            )
            return track.primary_artist_id, era.id
        if line.upc:
            release = self.release_by_upc.get(line.upc)
            if release is None or release.primary_artist_id is None:
                return None
            era = self.structure.era_contract_for(release.primary_artist_id, release.release_date)
            return release.primary_artist_id, era.id
        return None

    def compute_ledger(self, clean_lines: list[StatementLine]) -> list[LedgerRow]:
        config = self.config
        world = self.structure.world
        periods = config.periods

        lines_by_artist_period: dict[tuple[int, str], list[RevenueLine]] = defaultdict(list)
        for line in clean_lines:
            attribution = self.attribute(line)
            if attribution is None:
                raise ValueError(f"clean line {line.id} matches no catalog entry")
            artist_id, contract_id = attribution
            lines_by_artist_period[(artist_id, line.period)].append(
                RevenueLine(
                    contract_id=contract_id,
                    revenue_type=self.revenue_type_of_store[line.store],
                    territory=line.territory,
                    amount=line.gross_amount,
                    currency=line.currency,
                )
            )

        accounts_of_artist: dict[int, dict[str, Decimal]] = defaultdict(dict)
        for account in world.recoup_accounts:
            accounts_of_artist[account.artist_id][account.xcollat_group_id] = (
                account.opening_balance
            )

        charges: dict[tuple[int, str], dict[str, Decimal]] = defaultdict(dict)
        for advance in world.advances:
            period = advance.granted_at.isoformat()[:7]
            terms = self.governing_terms(advance.contract_id, period)
            bucket = charges[(advance.artist_id, period)]
            bucket[terms.account] = bucket.get(terms.account, ZERO) + money6(advance.amount)
        for expense in world.expenses:
            if not expense.recoupable:
                continue
            period = expense.incurred_at.isoformat()[:7]
            era = self.structure.era_contract_for(expense.artist_id, expense.incurred_at)
            terms = self.governing_terms(era.id, period)
            bucket = charges[(expense.artist_id, period)]
            bucket[terms.account] = bucket.get(terms.account, ZERO) + money6(expense.amount)

        ledger: list[LedgerRow] = []
        for artist in world.artists:
            state = ArtistState.initial(accounts_of_artist.get(artist.id, {}))
            for period in periods:
                # A deal governs only from its effective date (an MG clause must not pay
                # out before the deal exists); termination still accounts post-term.
                base_ids = [
                    c.id
                    for c in self.structure.eras[artist.id]
                    if c.effective_from <= period_end_date(period)
                ]
                terms_by_contract = {cid: self.governing_terms(cid, period) for cid in base_ids}
                outcome = compute_artist_period(
                    lines=lines_by_artist_period.get((artist.id, period), []),
                    terms_by_contract=terms_by_contract,
                    fx=config.fx_rates[period],
                    state=state,
                    period_charges=charges.get((artist.id, period), {}),
                )
                state = outcome.state
                ledger.append(
                    LedgerRow(
                        artist_id=artist.id,
                        period=period,
                        gross=outcome.gross,
                        recouped=outcome.recouped,
                        net_payable=outcome.net_payable,
                        balance_after=outcome.balance_after,
                    )
                )
        return ledger
