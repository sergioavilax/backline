# World Audit — five hand-verified artist-period calculations

This document is the Phase 1 spot-audit required by BUILD_PLAN: five artist-periods from
`truth.expected_ledger`, recomputed by hand, step by step. It doubles as the domain
explainer for reviewers who have never read a royalty statement.

Everything below is deterministic: `WORLD_SEED=20260805` (the default) always produces
exactly these artists, these numbers, and these edge cases. Reproduce any table here with
`make seed` and the SQL shown, or rebuild in memory via `python -m datagen fingerprint`.

**The money rules being audited** (BUILD_PLAN §0 invariant 1, implemented once in
`backline/royaltycalc/`):

- Every line amount converts to USD at the period's fixed rate and is quantized to
  6 decimal places, half-even (`money6`). Line royalty = `money6(usd × rate)`.
- Period gross = exact sum of line royalties (no re-rounding — sums of 6dp values are
  exact in Decimal).
- Recoupment: `balance_open = prior balance + this period's advances/recoupable
  expenses`; `recouped = min(gross_for_account, balance_open)`; earnings pool per
  *recoupment account* (cross-collateralized deals share one account).
- A minimum-guarantee top-up lifts the payable to the guarantee; the top-up itself is a
  recoupable advance.
- The artist-facing `net_payable` is rounded half-even **to cents, once, at final
  aggregation**. `gross`, `recouped`, `balance_after` stay at 6dp.

---

## Audit 1 — Cinders, 2025-08: the smallest possible complete check

Artist 35 ("Cinders") has exactly **two** clean statement lines in 2025-08, so every
digit can be verified with a pocket calculator. One deal (contract 572), streaming rate
**20% worldwide**, escalator at $15,000 (not reached), recoupment account `AC-00572`
with a **$12,600 opening balance**. August FX: JPY→USD `0.00638000`.

| line | store | territory | units | native gross | USD @ fx | × 20% | royalty (6dp) |
|---|---|---|---|---|---|---|---|
| 1 | Streamora | ES | 90 | 0.277828 USD | 0.277828 | 0.0555656 | **0.055566** |
| 2 | Vantage Music | JP | 938 | 367 JPY | 367 × 0.00638 = 2.341460 | 0.468292 | **0.468292** |

Line 1's rounding is the half-even policy in action: `0.0555656` → 6dp → `0.055566`.

- gross = 0.055566 + 0.468292 = **0.523858**
- balance_open = 12,600 (no new charges) → recouped = min(0.523858, 12600) = **0.523858**
- net_payable = to_cents(0) = **0.00**
- balance_after = 12,600 − 0.523858 = **12,599.476142**

```sql
SELECT gross, recouped, net_payable, balance_after
FROM truth.expected_ledger WHERE artist_id = 35 AND period = '2025-08';
--  0.523858 | 0.523858 | 0.00 | 12599.476142   ✓ all four digits-for-digits
```

## Audit 2 — Lior Okonkwo: an advance landing mid-year and recouping across three periods

Artist 45, one deal (contract 588, streaming 24% / download 26% / sync 52% / physical
8%, no escalator), zero opening balance, earning ~$1.4–1.7K/month. A **$2,900 advance**
is granted 2025-09-10 → it charges account `AC-00588` *in the September period, before
recoupment* (charges land on the balance first, then earnings recoup).

| period | gross | recouped | net_payable | balance_after | check |
|---|---|---|---|---|---|
| 2025-08 | 1,470.310955 | 0 | 1,470.31 | 0 | pre-advance: everything pays out |
| 2025-09 | 1,375.189321 | 1,375.189321 | 0.00 | 1,524.810679 | 2,900 − 1,375.189321 ✓ exact |
| 2025-10 | 1,414.965690 | 1,414.965690 | 0.00 | 109.844989 | 1,524.810679 − 1,414.965690 ✓ |
| 2025-11 | 1,516.695859 | 109.844989 | **1,406.85** | 0.000000 | payable raw = 1,516.695859 − 109.844989 = 1,406.850870 → cents ✓ |

November is the audit point for the rounding invariant: the raw payable
`1,406.850870` rounds half-even to `1,406.85` — one cent-rounding, at the end, never
per line.

## Audit 3 — Beatriz Romano: four un-pooled deals, a sync windfall, and an escalator

Artist 64 has **four sequential deals** (contracts 624–627), each with its **own**
recoupment account — she is *not* cross-collateralized. Opening balances: era-3
`AC-00626` = 7,800; era-4 `AC-00627` = 14,800; eras 1–2 clean. In-window advances add
7,200 (era 1), 6,900 (era 2), 4,300 (era 4).

Era attribution follows the recording, not the calendar: a track pays under the deal
governing its *original release date* forever (compilation re-appearances included).

**The October event**: a SyncBridge placement — `QZFBR2100131`, a 2021 recording (era
1, contract 624, sync rate **54%**) — for **$24,750**. Sync royalty = 24,750 × 0.54 =
**$13,365**, plus ~$356 of ordinary streaming across her catalog → gross
**13,721.006417**. Era 1's account was *clean* in October (its own $7,200 catalog
advance only lands 2025-12-09), so after era 2's account absorbed its usual ~$203,
**13,518.13 pays out** — while eras 3–4 sit ~$29K unrecouped that same month. Un-pooled
accounts do not raid each other; that is exactly what cross-collateralization would
change (see Audit 4). December then shows the reverse: the 7,200 advance lands and
`balance_after` jumps to 35,715.937859 = 28,900.235073 + 7,200 − 384.297214, exact.

**The escalator**: contract 624 bumps all rates **+3 pts after $30,000 cumulative** era-1
net receipts, measured at period start. The sync windfall pushed era-1 cumulative to
29,068 by 2025-12-01, and December's ordinary revenue crossed the line (30,030 by
month-end). Per the period-start rule the bump applies **from 2026-01**: era-1 streaming
earns 21% (18 + 3) from January onward — a mid-year rate change with no amendment,
recoverable only by doing the cumulative math.

```sql
SELECT period, gross, recouped, net_payable, balance_after
FROM truth.expected_ledger WHERE artist_id = 64 AND period IN ('2025-10','2025-12');
-- 2025-10 | 13721.006417 | 202.874909 | 13518.13 | 29111.894984
-- 2025-12 |   384.297214 | 384.297214 |     0.00 | 35715.937859
```

## Audit 4 — Felix & The Chorus: cross-collateralization, to the penny

Artist 15 signed two deals (527: 2024-12 →, 528: 2026-02 →) that share **one pooled
account `XC-0015`** (§6 of both contracts), opening balance **$900**. Every month's
earnings — under *either* deal — pay down the same balance:

| period | gross | recouped | net | balance_after |
|---|---|---|---|---|
| 2025-07 | 34.917655 | 34.917655 | 0.00 | 865.082345 |
| ... every month recoups in full ... | | | | |
| 2026-05 | 74.197578 | 74.197578 | 0.00 | 78.885363 |
| 2026-06 | 80.758171 | **78.885363** | **1.87** | **0.000000** |

June is the flip month: earnings 80.758171 against the remaining 78.885363 → recoup
stops at the balance, payable raw = 1.872808 → **$1.87**. Hand-check:
865.082345 = 900 − 34.917655 ✓ and the final chain telescopes exactly to zero.
The deal-2 signing (2026-02) did **not** open a fresh account — that pooling is the
whole point of the cross-collateral clause (and of the eval questions about these 12
artists).

## Audit 5 — The Stray Sirens: minimum guarantee as a recoupable advance

Artist 110 is a tail artist (~$5–20/month earnings) whose single deal (contract 720)
carries a **$1,200/period minimum guarantee**. The MG clause (§4) pays the floor every
month; everything paid above real earnings becomes recoupable balance:

| period | gross | recouped | net | balance_after | check |
|---|---|---|---|---|---|
| 2025-07 | 4.999797 | 0 | 1,200.00 | 1,195.000203 | top-up = 1200 − 4.999797 ✓ |
| 2025-08 | 11.524285 | 11.524285 | 1,200.00 | 2,383.475918 | 1,195.000203 − 11.524285 + 1,200 ✓ |
| 2026-06 | 13.393896 | 13.393896 | 1,200.00 | 14,267.456026 | twelve months of floors |

August shows the full waterfall in one row: earnings first recoup the July top-up
(recouped = 11.524285), the remainder is topped back up to 1,200, and the new top-up
joins the balance. After a year the artist has been paid $14,400 cash against $138.90
of earnings — and owes the difference against future royalties. This is why MG clauses
are negotiated carefully in the real world.

---

## Cross-checks that hold for all 150 artists × 12 periods

- `balance_after = balance_open + charges − recouped + mg_topup ≥ 0`, every row
  (property-tested in `tests/royaltycalc/test_properties.py`; DB-asserted in
  `tests/datagen/test_seed_integration.py`).
- `net_payable = to_cents(gross − recouped + mg_topup)`, cents, half-even, once.
- The truth engine consumed **clean** lines only: the 38 flaggable seeded anomalies are
  reporting corruption, not reality (D-005) — payable truth is unaffected by them.
- Same seed, same bytes: the world (all 17 tables + 842 rendered files) hashes to the
  committed fingerprint in `tests/golden/world_fingerprint.json`.

**Other seeded landmarks** (verified by tests, useful for poking at the world):
the JP territory carve-out is Astrid Dunes (artist 130, final deal excludes JP); the
mid-year termination is The Vivid Quarry (artist 119, deal ends 2026-01-31, post-term
revenue still accounted); the injection canary (§4.6) renders in contract FBR-C-00670
(Radiant Arcade) — PDF corpus only, never in canonical terms JSON.
