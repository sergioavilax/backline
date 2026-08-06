# Decisions

ADR-style log, numbered, append-only. Every judgment call not already specified by
[BUILD_PLAN.md](../BUILD_PLAN.md) gets an entry. Reserved by the plan: **D-001** (one
`royaltycalc` implementation — recorded when the library lands in Phase 1) and **D-002**
(structured-first governing-document retrieval — recorded in Phase 3).

---

## D-000 — Stack rationale (Phase 0)

**Status**: accepted · **Date**: 2026-08-05

**Context.** The repo must run on a reviewer's laptop from a cold clone in minutes, stay
legible to a single maintainer, and support the whole plan (agents, RAG, evals, UI) without
re-platforming later.

**Decisions.**

- **Python 3.12 + `uv`.** Lockfile-first, fast cold installs (matters in Docker layers and
  CI), one tool for env + resolution. `ruff` for lint *and* format (one tool, no
  black/isort split); `mypy --strict` from day one — retrofitting strictness later is
  strictly worse.
- **FastAPI + uvicorn.** Async-native (the agent loop, SSE streaming in Phase 6), Pydantic
  models shared with tool schemas, OpenAPI for free (the plan's Phase 6 checkbox).
- **Postgres 16 + pgvector as the only datastore** (`pgvector/pgvector:pg16` image).
  Relational facts, vector search, FTS, staging queues, and trace storage in one system —
  no Redis/queue/vector-DB sprawl a reviewer has to boot. This is also a deliberate match
  to the target job spec ("proficiency in PostgreSQL").
- **Raw-SQL migration runner over Alembic** (`backline/db/migrate.py`, ~100 lines:
  `migrations/*.sql` in filename order, versions recorded in `schema_migrations`, one
  transaction per file). The plan's schemas are hand-written SQL across four Postgres
  schemas with index/COPY tuning; Alembic's autogenerate adds nothing on SQLAlchemy-free
  DDL, and the runner is small enough to read in one sitting. Cost: no auto-downgrades —
  acceptable, since migrations here are append-only and the dev reset path is
  `docker compose down -v`.
- **asyncpg** for DB access (no ORM). The repo's queries are hand-written SQL by design
  (the Analyst's SQL tool, COPY-based loads); asyncpg is the fastest async driver and maps
  `NUMERIC` → `Decimal` natively (invariant 1).
- **Next.js 15 + TypeScript + Tailwind, pnpm** — current stable of the stack the job spec
  names (React/Next.js). Standalone output keeps the Docker image node_modules-free.
- **Docker Compose with a one-shot `init` service** (migrations now; seed/embed appended in
  Phases 1/3) — encodes "clone → up → working" as the only supported production
  environment per BUILD_PLAN §0.
- **CI on GitHub Actions**: lint+type / pytest-with-Postgres-service / ui build as separate
  jobs for parallelism and readable failures; eval-regression job stubbed behind a
  step-level secret check (secrets aren't readable in job-level `if`), so forks stay green.

**Alternatives considered.** Poetry/pip-tools (slower, two tools); Django/Flask (no native
async loop + SSE story); SQLite (no pgvector/FTS/NUMERIC parity with the plan);
Alembic (see above); npm/yarn (pnpm is faster and strict about phantom deps).

**Consequences.** One datastore to operate; `uv.lock` + `pnpm-lock.yaml` pin everything;
migration discipline is manual (append-only, reviewed in PRs).

---

## D-001 — One implementation of royalty math (Phase 1; reserved by BUILD_PLAN §0)

**Status**: accepted · **Date**: 2026-08-05

**Decision.** `backline/royaltycalc/` is the single implementation of rate resolution,
escalators, FX, recoupment, cross-collateral pooling, minimum guarantees, and the
rounding policy. The datagen truth engine imports it to compute `truth.expected_ledger`;
the Phase 3 `calc_royalties` tool will import the same functions. It also owns the
*interpretation* of contract terms: the canonical JSON in `label.contract_terms` is
parsed and amendment-superseded by `royaltycalc.terms` (`parse_terms_doc` /
`resolve_terms`) — datagen writes docs through the same parser the runtime reads with.

**Consequence.** Evals measure whether *agents* retrieve the right terms and call the
calculator correctly — never whether two arithmetic implementations agree. The library
is stdlib-only (Decimal + dataclasses), 100% branch-covered, with Hypothesis property
tests for the two plan-named invariants (allocations sum to gross − deductions;
balances never double-recoup).

---

## D-003 — Royalty semantics not pinned by BUILD_PLAN (Phase 1)

**Status**: accepted · **Date**: 2026-08-05

Judgment calls the plan's §3.2 left open, now fixed in code and prose (contract PDFs
state each rule in their clauses, so Counsel can retrieve them):

- **Escalators evaluate at period start.** A tier crossed *during* a month bumps the
  following months, never its own — no intra-month rate splitting. Tiers state *total*
  bumps (highest crossed tier applies; not additive). Cumulative excludes carve-out
  territories and, per contract, counts observable revenue from 2025-07 (pre-history
  sits in `opening_balance`, not in escalator state).
- **Minimum guarantee = recoupable floor.** Payable is lifted to the MG each period;
  the top-up is an advance against future royalties (joins the account balance).
- **Post-term accounting.** A terminated deal's terms keep governing revenue on its
  recordings after `effective_to` (industry-standard master-follows-the-deal).
- **Era attribution follows the recording.** A track pays under the deal governing its
  original release date, forever — including compilation re-appearances. Physical
  (blank-ISRC) lines attribute by UPC at release level.
- **Expenses charge the era account at `incurred_at`**; advances charge their stated
  contract's account. Both land on the balance *before* that period's recoupment.
- **Statement lines are label net receipts.** Distributor fees are already off the top;
  rate cards apply to line gross as reported (defined as "Net Receipts" in §1 of every
  contract).

---

## D-007 — Provider layer: official SDK for Anthropic, httpx for OpenAI-compat (Phase 2)

**Status**: accepted · **Date**: 2026-08-06

**Context.** BUILD_PLAN §4.1 requires an `AnthropicProvider` (Messages API tool use,
streaming, retries with jittered backoff on 429/529, `anthropic-version` pinning) and an
`OpenAICompatProvider` for any OpenAI-format endpoint, both normalizing to one internal
wire shape.

**Decisions.**

- **AnthropicProvider is built on the official `anthropic` SDK** (`AsyncAnthropic`),
  not hand-rolled HTTP. The SDK pins `anthropic-version`, retries 408/409/429/5xx/529
  and connection errors with jittered exponential backoff (`max_retries=4` here), and —
  because the provider always streams and accumulates via `get_final_message()` —
  assembles partial `input_json_delta` tool-argument fragments (the §9 pitfall).
  Hand-rolling those three would mean re-testing solved problems. The provider is still
  fully unit-testable offline: the SDK accepts an injected `http_client`, so tests
  drive it with `httpx.MockTransport` serving canned SSE (including a tool-use argument
  split mid-`\u` escape) and assert both the outbound wire shape and the normalization.
- **OpenAICompatProvider speaks raw httpx** — the endpoint is by definition
  not Anthropic (vLLM, OpenAI, together), the surface is one POST, and a dependency on
  the `openai` package would drag a large SDK in for a thin shim. It owns its
  retry/backoff (429/5xx/transport errors, exponential with jitter). Jitter entropy
  comes from `secrets`, not `random` — invariant 4's "no bare `random` calls" stays
  cleanly greppable — and both the sleeper and jitter are injectable so retry tests run
  in microseconds.
- **Normalization boundary**: internal `Message`/`ToolCall`/`CompletionResult` types
  (`providers/base.py`) are the only shapes the runtime sees. Notable mappings:
  consecutive internal `tool` messages merge into one Anthropic user turn of
  `tool_result` blocks (parallel calls must be answered in a single message);
  OpenAI-format tool arguments arrive as JSON *strings* and are parsed here, with
  unparseable arguments surfaced as a `ProviderError` (local-model tool-JSON mangling is
  a named Phase 7 risk — better loud than guessed); `stop_reason`/`finish_reason`
  collapse to five internal values. `temperature` is omit-when-`None` because current
  Anthropic models reject explicit sampling params.
- **Registry mediates model → provider**: `config/models.yaml` maps each model id to
  `{provider, context_window, USD/Mtok in/out}`. Prices are quoted strings parsed to
  `Decimal`; a bare YAML float fails loading (money is never float). Mock models
  (`mock-sonnet`, `mock-haiku`) are registered at real-tier prices so keyless tests
  exercise genuine budget arithmetic.

**Consequences.** Live-API behavior is delegated to a maintained SDK and verified once
by a human via `pytest -m live` (excluded by default); everything else runs offline.
The `anthropic` package is a runtime dependency; `httpx` moved from dev to main deps.

---

## D-008 — Trace persistence: insert spans on start, complete on end (Phase 2)

**Status**: accepted · **Date**: 2026-08-06

**Context.** §4.7 wants spans in Postgres + JSONL + a live feed, with cost/token attrs.
The first cut inserted `app.spans` rows on span *end* — and the integration test
failed immediately: children end before their parents, so `spans.parent_id`'s self-FK
referenced a row that didn't exist yet.

**Decision.** `PostgresSink` inserts the row on `span_start` (`ended_at` NULL) and
completes it on `span_end`. Parents always *start* before children, so FK order holds
— and in-flight spans are queryable mid-run, which the Phase 6 Trace Inspector wants
anyway when re-attaching to a running agent. The JSONL sink stays one durable line per
event (`run_start`, completed `span_end`s, `run_end`) in a per-run file; the in-proc
`TracePubSub` carries both start and end events for the future SSE feed. Attrs use
OTel `gen_ai.*` naming; serialization goes through the repo's one JSON encoder
(`jsonutil`, now also UUID + ISO datetime), so a Decimal cost is a string in JSONB and
JSONL, never a JSON float. Run cost lands in `app.runs.cost_usd NUMERIC(12,6)` as
native Decimal.

**Alternatives rejected**: buffering spans and flushing on run end (loses the trace on
a crash — precisely when it matters); dropping the FK via a new migration (weakens the
schema to accommodate a sink bug); deferrable constraints (hides write-order problems
instead of fixing them).

---

## D-009 — Runtime-loop semantics BUILD_PLAN §4.2 leaves open (Phase 2)

**Status**: accepted · **Date**: 2026-08-06

Judgment calls in `core/runtime.py`, now fixed:

- **Budget trips at iteration boundaries** (the plan's `while ... cost < budget`
  semantics): the check runs before each LLM call; a run already over budget ends
  `status=exhausted` with a run-level `guardrail` span. A final answer produced by the
  call that *crosses* the budget still completes — the cap prevents further spend, it
  doesn't retract finished work.
- **Tool failures return to the model, not to the caller**: invalid args (Pydantic),
  unknown tools, timeouts, and handler exceptions all become `is_error` tool results
  plus a traced incident/status — the model gets a chance to correct itself within its
  iteration budget. Only `ProviderError` ends the run (`status=error`); programming
  errors propagate.
- **Cost accounting reuses the one rounding policy**: each call's cost is
  `money6(tokens × price / 1M)` via `royaltycalc.rounding` — API spend is money, so it
  follows invariant 1 rather than growing a second quantization rule.
- **Oversize tool results** (est. tokens ≈ chars/4 — same offline convention as
  datagen's corpus estimate) are summarized by the agent's `utility_model` when
  configured, else deterministically truncated; either way a `compression` span records
  method, sizes, and (for the model path) usage + cost, so shrunken context is never a
  silent lie about what the model saw.
- **Dedup key is `(tool, content)`**: identical bytes from *different* tools are
  coincidence, not duplication; repeats become a short pointer to the first result's
  index.

---

## D-004 — Recoupment accounts: one row per account, key referenced from terms

**Status**: accepted · **Date**: 2026-08-05

**Context.** `label.recoup_accounts(artist_id, xcollat_group_id, opening_balance)` (§3.3)
must model both pooled (cross-collateralized) and independent multi-deal artists, but has
no contract linkage column.

**Decision.** One row per *account*, PK `(artist_id, xcollat_group_id)`, where
`xcollat_group_id` is the account key (`XC-{artist}` pooled / `AC-{contract}`
independent). The contract→account linkage lives in the canonical terms JSON
(`advances_recoupment.account`) and is restated in §4/§6 of the rendered PDF — the
linkage is deal data, so it belongs in the deal. Amendments never move an account.
Cross-collateralization is then *no special case in the engine*: pooling is simply two
contracts naming the same account.

**Alternative rejected**: a separate `contract_accounts` join table — adds a table the
plan doesn't name for information the terms already carry.

---

## D-005 — Anomaly semantics: the clean world is the payable truth

**Status**: accepted · **Date**: 2026-08-05

**Decision.** Anomalies (§3.4) are corruptions of the *reporting*, generated
registry-first: the plan picks targets, registers them in `truth.anomaly_registry`, and
then the corruption is applied to what statements/CSVs/DB carry. The truth engine
consumes the clean set only. Per kind: duplicates/unknown-ISRC/negative-units/period-
bleed/territory-spikes are *injected* lines (excluded from payable truth);
`currency_mismatch` corrupts the currency field of a real line (meridian only — its
dialect has an explicit currency column, so the lie is detectable); `dashboard_gap`
corrupts the *dashboard side*, leaving statement money authoritative. The two borderline
cases carry `expected_flag_kind = NULL` (flagging them is a precision failure): a 3.4%
dashboard gap inside the 5% tolerance, and a genuinely legit first-territory line at
~1.6x median volume (which *is* part of payable truth). `emit-period` months inject a
few anomalies unregistered — their line ids don't exist until ingestion, and the demo
month is Reconciler material, not eval material.

---

## D-006 — Determinism: named RNG streams + a committed content fingerprint

**Status**: accepted · **Date**: 2026-08-05

**Decision.** All randomness derives from `WORLD_SEED` through named
`numpy SeedSequence` streams (`datagen/rng.py`, the only construction site — grep-
enforced by test): a *world stream* for structure and one *period stream* per month, so
`emit-period 2026-07` reproduces its month without replaying the seeded window.
Determinism is pinned by a committed fingerprint (`tests/golden/world_fingerprint.json`):
sha256 per table over canonically-serialized rows plus sha256 of every rendered file
(ReportLab runs in `invariant` mode, so PDFs are byte-stable). Three views must agree —
the in-memory build (unit test), a fresh build (same test), and the loaded Postgres
content (integration test) — so accidental world drift fails CI before it silently moves
the answer key. `truth.expected_ledger.net_payable` stores the cent-rounded *value* at
the column's 6dp scale.
