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

---

## D-002 — Structured-first governing-document retrieval (Phase 3; reserved by BUILD_PLAN §4.4)

**Status**: accepted · **Date**: 2026-08-06

**Context.** "What is X's sync rate?" is only answerable from the clauses that *govern*
X on the date in question. Which documents those are is not a similarity question — it
is a relational fact: base contracts effective by that date (termination does not
un-govern; post-term accounting, D-003), plus effective amendments, minus the base
sections those amendments replaced.

**Decision.** Resolve governance in SQL *before* any text ranking
(`backline/rag/governing.py`), then run hybrid retrieval only over governing chunks.
Amendment supersession is applied at clause granularity via a fixed section→clause map
(`term_territory→§2, royalties→§3, advances_recoupment→§4` — pinned against the
renderer by test), so a superseded rate clause is structurally unfindable, not merely
outranked. Historical questions opt in explicitly (`include_history=true`), which lifts
both the supersession exclusion and the effective-date cutoff.

**Mechanics around it.** Chunks *are* clauses (`rag.contract_chunks`, parsed from the
renderer's deterministic `.txt` sidecars — no token windows over legalese), keyed
(contract_id, clause_no, part) so citations are structural. The two ranking legs are
Postgres FTS (`ts_rank_cd` over a weighted tsvector: heading A, body B) and pgvector
cosine, fused with RRF (k=60) over 50 candidates per leg; the cross-encoder reranks
only the fused top-30 (§9), toggleable via `RERANK`. The `rag` schema is *not* in the
SQL tool's allowlist — agents reach chunks only through `search_contracts`/`read_clause`.

**Alternative rejected**: metadata-filtered vector search over *everything* with
recency boosting. Freshness is not recency — an 2019 base §5 governs today while its
2024 §3 may be dead; a boost can only make stale text less likely, never wrong. With
supersession already structural in the world (`label.amendments.replaced_sections`),
re-deriving it statistically trades a correct join for a tunable error rate. The cost
of the chosen design — retrieval quality now depends on an upstream SQL filter being
right — is covered by dedicated governing-filter tests rather than eval vibes.

**Measured consequence** (offline deterministic stack): artist-scoped retrieval over
governing docs reaches MRR 0.387 / recall@10 0.85, while the same queries over the
full corpus with the artist only *named in the text* collapse to MRR ~0.006 — the
strongest empirical argument that entity/govern scoping belongs in structure, not in
the embedding.

---

## D-010 — Agent ingestion is staging-only; approval promotes (Phase 3)

**Status**: accepted · **Date**: 2026-08-06

**Context.** §4.3's `ingest_statement` says "parse a `/data/inbox` CSV → *staged raw
lines* + parse report", but §3.3 defines no staging table for lines, and
`label.statements.status` has a `received → ingested` lifecycle someone must drive.

**Decision.** Invariant 5 wins, literally: everything an agent parses lands in a new
`staging.ingested_lines` table (migration 0003); `label.statement_lines` and
`label.statements.status` are never touched by any tool. A statement stays `received`
until a human approves the submitted batch — promotion (staged lines → label, status
flip) is the Phase 6 review action. Consequences embraced: `match_lines` and
`calc_royalties(include_staged=true)` read staged lines for received statements and
label lines for ingested ones, and Analyst SQL over `label.statement_lines` never sees
un-reviewed money (staging is separately queryable). Re-ingestion replaces the
statement's own staged rows (idempotent); re-seeding truncate-cascades staged lines
away with their statements.

**Alternative rejected**: writing parsed lines straight into `label.statement_lines`
with a status flip — simpler plumbing, but it makes "agents propose, humans approve"
false for the highest-volume write in the system.

---

## D-011 — Embedding stack: optional extra, recorded model, deterministic offline twin (Phase 3)

**Status**: accepted · **Date**: 2026-08-06

**Decisions.**

- **`sentence-transformers` is an optional extra** (`uv sync --extra embed`), not a core
  dependency: it drags torch in, and PyPI's linux torch wheels are CUDA builds (~5 GB
  installed). Keyless CI and unit tests never load it.
- **The offline twin is a real implementation, not a mock**: `HashingEmbedder`
  (sha256 feature-hashed unigram+bigram bag-of-words, signed 384-dim buckets, sublinear
  tf, L2-normalized) and `LexicalReranker` (query-term coverage + bigram bonus) are
  deterministic, dependency-free stand-ins that measure *lexical* similarity honestly.
  Tests, keyless CI, and model-less environments run them (`EMBED_MODEL=hash`,
  `RERANK_MODEL=lexical`); the retrieval probe labels which stack produced its numbers.
- **The chunk store is single-model by construction**: each row records
  `embedding_model`; queries must embed with the model that built the store (mismatch
  raises), and switching models re-embeds everything. A store with no embeddings
  degrades to FTS-only search, recorded in the result — so compose init runs
  `rag.embed --best-effort` and a cold boot without model egress still yields a fully
  working (FTS-only) stack instead of a dead one.
- **The Docker image ships without the extra** for now (image-size discipline; CUDA
  wheels). Full hybrid embeddings build via host-side `make embed`. The intended
  follow-up, on a network that can reach it, is re-locking torch against the PyTorch
  CPU wheel index and flipping `--extra embed` on in `docker/api.Dockerfile` — a
  two-line change noted there.
- The ivfflat cosine index is created by the embed job *after* bulk embedding (then
  `ANALYZE`), and retrained (drop + recreate) whenever new vectors were written —
  never by the migration, where it would train on an empty table (§9 pitfall).

---

## D-012 — Runtime calculator: DB assembly semantics (Phase 3)

**Status**: accepted · **Date**: 2026-08-06

Judgment calls in `backline/tools/ledger.py` / `calc.py` that §4.3 left open:

- **The tool computes from *reported* lines** (label + optionally staged), not from any
  cleaned set — the Reconciler's whole job is deciding what to exclude
  (`exclude_line_ids`). Structurally impossible lines (non-positive units, negative
  amounts) are auto-excluded and *reported*, because the engine rightly refuses
  negative money. Pinned by test: every artist untouched by line-level anomalies
  reproduces `truth.expected_ledger` exactly (all four columns, microdollar precision,
  full 12-period chain) from Postgres alone — the D-001 single-implementation claim,
  proven from the DB side; a sensitivity canary asserts corrupted artists *diverge*.
- **Attribution is re-derived relationally**: ISRC → track → era base contract by the
  track's origin release date (min release date across its releases — compilations
  postdate origins by construction); blank-ISRC lines by UPC → single-artist release.
  Era selection = last base with `effective_from ≤` the origin date (D-003 semantics).
- **Two modes, one tool**: ledger (real history through a period, full waterfall) and
  spot (hypothetical rows under terms as of a date, with true escalator state computed
  by running the chain through the prior period; output is labeled PRE-RECOUPMENT).
- **Store → revenue-type classification reads `datagen/world.yaml`** via
  `datagen.config` — the label's store reference lives beside its feed definitions,
  exactly as it would at a real label. Alternative rejected: a `label.stores` table
  would be cleaner SQL but changes seeded content, forcing a golden-fingerprint
  regeneration mid-phase for a lookup the runtime can read from config; revisit if a
  later phase regenerates the golden anyway.
- **`emit-period` now also records its month's FX rows** (from world.yaml, idempotent
  insert) — staged-period math needs FX, and the seeded window's fingerprint is
  untouched because seeded periods already exist.
- Tools learn the proposing run via a `ContextVar` (`core/runcontext.py`) the runtime
  sets around each run — `staging.*_by_run` / `app.notes.created_by` stamping without
  widening the tool-handler signature.

---

## D-013 — Reconciler heuristics as deterministic tools; guardrails gain a flag-don't-block channel (Phase 4)

**Status**: accepted · **Date**: 2026-08-06

**Context.** Phase 4's Reconciler workflow names "flag heuristics (tolerance rules
per anomaly kind)" as a stage between calc and `submit_batch`, and §4.6 requires the
injection defense to *flag* suspicious document content while the agent still sees
it. Neither fits the Phase 2/3 guardrail shape (pre-execution checks that deny), and
asking a model to re-derive 5%-tolerance arithmetic per run would make precision a
matter of luck.

**Decisions.**

- **Tolerance rules live in a tool, not in prose**: `scan_anomalies(period,
  statement_id?)` (`backline/tools/scan.py`) implements one deterministic rule per
  §3.4 kind — duplicate `line_hash` within a statement (lowest id kept), unknown
  ISRC vs catalog, currency vs the feed's dialect reference (world.yaml via
  `datagen.config`, the D-012 precedent; GBP territories honored), negative
  units/amounts, line-vs-statement period bleed, first-territory streaming lines at
  ≥ 4x the track's median historical per-line units on that store (`SPIKE_FACTOR`),
  and statement-vs-dashboard divergence beyond the configured 5% (aggregated by
  *statement* period, anchored to the largest contributor — exactly how the
  reference is built). Within-tolerance measurements are *reported, not flagged* —
  the two seeded borderline cases fall below these thresholds by construction, and a
  test pins exact set equality of `(kind, line_id)` against
  `truth.anomaly_registry` across all 12 periods: 100% recall, zero extras. The
  agent reviews candidates and owns the final flag list (it can drop, never
  silently gain).
- **Batch allocations are one tool call**: `compute_allocations(period, ...)` loops
  `compute_ledger_slice` (the one engine, D-001) over every artist with reported
  lines in the period under bounded concurrency (~7s for 149 artists), honors
  per-source exclusions, and applies a **materiality floor** (`min_net_payable`,
  default $0.01) — zero-payable (typically unrecouped) artists are counted and
  aggregated, not listed, so `submit_batch` payloads stay reviewable and the
  coverage stays visible. A test pins allocations == `truth.expected_ledger` for
  every clean artist once registry exclusions are applied.
- **Exclusions are per-source**: label and staged line ids are separate sequences
  that can collide numerically, so `exclude_line_ids` (label) and a new
  `exclude_staged_line_ids` flow through `calc_royalties`,
  `compute_allocations`, and the ledger — an exclusion can never silently hit the
  other source's line. (Latent Phase 3 wrinkle, fixed while the surface grew.)
- **Guardrails gain `ResultCheck`** — post-execution policies over
  `(tool_name, result_text)` that *flag without blocking*: the runtime records the
  incident as a `guardrail` span, marks the tool span, and prefixes the result with
  a one-line notice before the model sees it. The injection detector
  (`backline/agents/injection.py`) registers here for `search_contracts` /
  `read_clause` only (SQL/calculator output is label-controlled data, not
  documents); its regex families (role/override markers, instruction overrides,
  prompt/answer-key exfiltration, approval coercion) catch the seeded canary and,
  by corpus sweep test, nothing else in all 2,961 chunks. Retrieval tools fence
  quoted corpus text in `<document>` tags so the trust boundary is visible in every
  transcript.

**Alternatives rejected**: prompting the model to hand-write tolerance SQL per run
(unreproducible precision, token-expensive); blocking suspicious documents outright
(the §4.6 eval needs the model to *see and refuse*, and legal text can trip
heuristics — flag-and-annotate degrades gracefully); a `label.stores`-style
reference table for feed currencies (changes seeded content mid-phase for data the
config already carries — same call as D-012).

---

## D-014 — Agent assembly conventions: text-protocol finalizers, two-run dispatch, prompt hashing (Phase 4)

**Status**: accepted · **Date**: 2026-08-06

Judgment calls in `backline/agents/` that BUILD_PLAN Phase 4 left open:

- **Typed answers parse from the final text, not from a special tool.** The §4.2
  termination contract (text turn without tool calls → `FinalAnswer`) stays
  untouched: citations are extracted structurally (`FBR-[CA]-NNNNN §N` patterns,
  deduped, order kept — the prompts require inline citations in exactly that
  shape); a first line `ABSTAIN: <reason>` is the typed abstention; the
  Reconciler ends with `BATCH: <id|none>` / `FLAGS: <summary>` lines parsed into
  `ReconcilerAnswer(batch_id, flags_summary)` (a `FinalAnswer` subclass). T1/T2
  scoring gets deterministic fields; a finalize-tool would have complicated the
  loop for no scoring gain.
- **Prompts are files; the hash is the version.** `backline/agents/prompts/*.md`
  load verbatim as system prompts; each `AgentSpec` carries
  `trace_attrs={"prompt_sha256": <12-hex>}` which the runtime merges into run meta
  — eval results pin to prompt versions with zero templating. Dynamic context
  (recalled notes) deliberately rides in the *user turn*, never the system prompt,
  so the hash stays honest.
- **The router is its own traced run.** `route()` makes one forced `route` tool
  call (Haiku-class tier, `ROUTER_MODEL`) inside a run named `router` — front-door
  cost and verdicts stay separately inspectable, and `route_and_run` produces two
  runs per message (router + agent) by design. Below
  `ROUTER_CONFIDENCE_THRESHOLD` (default 0.6) the decision downgrades to `clarify`
  carrying the shadowed suggestion in `reason`; unparseable/missing tool calls
  degrade to `clarify` with confidence 0 — the front door never guesses and never
  crashes on model judgment (provider outages still raise).
- **Note auto-recall is router-keyed and user-visible.** The router reports artist
  names verbatim; dispatch resolves them (exact-first, misses skipped silently),
  pulls up to 5 notes per artist, and prepends a fenced `<recalled_notes>` block to
  the user message — what the model saw is exactly what the trace shows. Notes are
  trusted-ish label data; user-spoofable fencing is acceptable at this trust
  boundary and revisitable when sessions arrive (Phase 6).
- **Model policy is three settings** (`PLANNER_MODEL`, `UTILITY_MODEL`,
  `ROUTER_MODEL`) rather than per-agent env knobs; `build_agent(model=...)`
  overrides exist for tests and the Phase 7 sweep. The Reconciler gets workflow
  headroom in code (2x iterations/budget, 120s tool timeout for the batch
  calculator, 4K-token results so allocation tables survive verbatim) — config
  would multiply env vars for limits only one agent needs.
- **Session-memory summarization stays put until Phase 6**: the §4.5 scope-1
  summarizer hook exists since Phase 2, but sessions (and therefore a place to
  construct them) arrive with the API — wiring the utility model in belongs there.
  The §4.5 scope-3 tail (entity auto-recall) shipped here as planned.

---

## D-015 — Eval suite as a golden artifact; output contracts; pinned agents (Phase 5)

**Status**: accepted · **Date**: 2026-08-06

**Context.** §5.2 wants ~130 questions "generated + hand-authored" from the answer
key, content-hashed, with deterministic T1 scoring and mechanical T2 trace
assertions. Several load-bearing details were unspecified: where expected answers
come from, how a prose answer becomes a scorable value, which agent fields each
question, and how corruption-affected artists interact with money questions.

**Decisions.**

- **The suite is a golden artifact, generated offline.** `evals/generate_suite.py`
  builds the world in memory (`datagen.assemble.build_world`, no database) and
  derives every expectation from the same objects the DB is seeded from
  (`world.ledger` == `truth.expected_ledger`; ids are explicit, so in-memory line
  ids == Postgres ids). The committed `evals/suites/core.json` must reproduce
  byte-for-byte (`python -m evals generate --check` in CI + a test) — the world
  fingerprint discipline applied to the question set. Selection variety comes from
  a dedicated seeded stream (`SeedSequence([WORLD_SEED, 1005])` — disjoint from
  datagen's streams by spawn key).
- **Hand-authored cases carry prompts, never numbers.** Each of the 25 committed
  hard cases (`suites/hand_authored.json`) names a *resolver* in the generator
  that binds its `{placeholders}` and derives the expectation from the answer key
  at generation time — a hand case can go stale in prose but never in arithmetic.
  Every generated expectation is validated at generation (`GenerationError` on
  rate-zero, ambiguous titles, unreachable quotas...), and Postgres-backed tests
  re-verify the committed suite against the seeded DB (reference SQL reproduces
  expected values; ledger fields match `truth.expected_ledger`; flag sets match
  the registry; paid-over sets match truth).
- **Questions state their output contract.** Prompts end with an explicit
  final-line protocol (`ANSWER: $<amount>`, `ANSWER: YES/NO`, `FLAG: <kind>
  <source>:<line_id>` lines...) appended uniformly by the generator. T1 extraction
  is therefore mechanical (last `ANSWER:` line wins); ignoring the contract is a
  named failure (`no_answer_line`), not a fuzzy match. Abstention questions wear
  the suffix of the kind they masquerade as — they must look like normal
  questions. The typed `ABSTAIN:` first-line protocol (D-014) is what scores;
  prose reluctance does not.
- **Eval questions pin their agent.** The runner drives the named agent directly —
  no router in the eval path. The suite measures agent competence per category;
  the router has its own unit/live tests, and §5.2 defines no routing category.
  Reconciler workflow questions (reconciliation, multi_step) carry the agent the
  workflow belongs to.
- **Anomaly-tainted artists cannot anchor money questions.** An artist whose
  statement lines carry registered non-borderline corruption (any kind except the
  dashboard-side `dashboard_gap`, D-005) has a DB-computed ledger that legitimately
  diverges from truth until the Reconciler excludes the corruption — money
  questions target the other ~130 artists. Pay-over-threshold sets (multi_step) are
  additionally *boundary-checked*: no tainted artist's payable may sit within its
  worst-case corruption shift of the threshold (shift bound = Σ of the artist's
  corrupted-line values × max FX × a ceiling rate, cumulative through the period),
  so exclude-vs-keep handling can never flip set membership.
- **A question's score is the minimum of its tier scores.** A correct number
  produced by a forbidden process (mental math, denied SQL) fails, as does a
  beautifully-cited wrong number. Category score = 100 × mean of question scores;
  reconciliation questions score F1 (precision/recall vs the registry) with the
  two borderline non-flags reported by name. T2 asserts over the span tree — a
  `sql_policy` guardrail denial *is* the violation even though the tool blocked
  it (the attempt shows intent; invariant 3's eval face).
- **T3 is platform-only and blind to expectations.** The judge
  (`evals/judges/rubric_v1.md`, content-hashed; model + rubric hash recorded per
  result) grades faithfulness-to-citations, clarity, and hedging on 1-5. It sees
  the question, the answer, and the verbatim text of cited clauses fetched by the
  harness — never the expected answer (T1 owns accuracy) and never a baseline
  answer (B0/B1 cannot cite structurally; grading their faithfulness to nothing
  would be noise). Baseline tracks record T2 as `not_applicable` — never counted,
  never a violation.

**Alternatives rejected**: generating from the seeded Postgres (couples suite
generation to a DB and hides answer-key drift); LLM-extracted answers (a second
model grading the first — unfalsifiable); routing eval questions through the
router (conflates two measurements; doubles cost); letting money questions hit
tainted artists with "the agent should exclude corruption" (turns T1 into a
reconciliation test with an under-specified expected value).

---

## D-016 — Regression gate: keyed baselines, bootstrap-pass, a mock baseline with teeth (Phase 5)

**Status**: accepted · **Date**: 2026-08-06

**Context.** §5.4: compare runs against a committed `evals/results/baseline.json`,
fail CI when a category drops >3 pts or T2 violations appear — but no live run
exists until the human executes the first budgeted eval, and a gate that only ever
runs behind a secret is a gate nobody has seen fire.

**Decisions.**

- **Baseline entries are keyed `(model, track, subset)`** and pin the
  `suite_hash` they were recorded against. A run whose suite hash differs fails
  the gate outright ("stale baseline") — changing the question set requires a
  conscious re-baseline in the same PR, exactly like the world fingerprint.
  `--write-baseline` upserts an entry wholesale from a run summary.
- **No matching entry → bootstrap pass, loudly.** A gate with no reference would
  either block forever or invent numbers; it passes with an explicit BOOTSTRAP
  banner telling the operator to record one. Budget-exhausted partial runs can
  never clear the gate (a subset score is not a comparable score).
- **The committed baseline ships with the three mock-smoke entries** (platform /
  b0 / b1 × `mock-sonnet` × smoke subset, all 100s from deterministic
  perfect-agent scripts). `make eval-smoke` runs the whole harness keylessly on
  every PR — real agents-on-mock, real tools, real Postgres, real scorers — and
  gates against those entries, so the gate mechanism itself executes with teeth
  on every PR, not just when a key is present. A test sabotages one scripted
  answer and asserts the committed gate trips (the DoD's gate-of-the-gate, at
  system level). Live `claude-sonnet-5` entries land when the human runs the
  first budgeted eval and commits `--write-baseline` output (the same deferred-
  live-artifact protocol as Phases 2–4).
- **CI shape**: the `test` job gains the suite-drift check and keyless
  `make eval-smoke`; the `eval-regression` job becomes real — Postgres service,
  `--extra embed` (CPU wheels, D-011), seed + real embeddings, `evals run
  --gate-subset --model claude-sonnet-5 --budget 5.00 --yes --gate` — with the
  step-level secret gate so forks stay green. It runs on the GitHub-hosted runner
  until a self-hosted one is registered (BUILD_PLAN names self-hosted; a
  `runs-on` job comment marks the one-line switch — a job pinned to a
  non-existent runner would queue forever, which is worse than a slower hosted
  run). `nightly-evals.yml` runs the full suite on main on the same pattern.
- **The gate subset is how §5.4's $5 budget is honored**: 43 flagged questions
  (every hand-authored hard case + a per-category quota of generated ones,
  adversarial included) project comfortably under $5 on the Sonnet tier, while
  the full 133-question, three-tier run projects past it — that one belongs to
  the nightly workflow and the human's budgeted full runs.

**Consequence.** Until the first live baseline lands, a live regression is caught
only by the absolute rules (T2 violations, partial-run refusal) — accepted, and
visible in the gate output as bootstrap language rather than silent green.

## D-017 — Dated price schedules in the model registry (eval run 2b9f39fb diagnosis)

**Status**: accepted · **Date**: 2026-08-06

**Context.** `config/models.yaml` carried claude-sonnet-5 at the standing $3/$15
sticker while Anthropic bills launch-intro $2/$10 through 2026-08-31 (UTC) — the
file's own comment admitted the discrepancy "so cost numbers stay comparable".
Run 2b9f39fb metered $16.74 against ~$11.16 actually billed (exactly 1.5×), and
the overstatement propagated beyond reporting: every per-run budget guardrail
(`RUN_BUDGET_USD`, checked against the meter each iteration) effectively shrank
to ⅔ of its real-money intent, squeezing long Reconciler workflows toward
`exhausted`, and the suite-level budget compared real dollars against inflated
meter dollars.

**Decisions.**

- **A model may carry a dated `pricing` schedule** instead of flat fields:
  ordered tiers, each billing through its inclusive UTC `through` date, exactly
  one open-ended final tier (validated loudly: missing terminal tier, unordered
  dates, flat+schedule together, float prices all refuse to load).
- **`ModelRegistry.load(on=...)` resolves the tier for the load date** (default:
  today, UTC) and records the choice in `ModelInfo.price_note`; the eval runner
  banner prints the resolved price. The Sept 1 transition happens on the
  calendar and out loud — no constant edits, no silent flip; tests pin both
  sides of the boundary.
- **Mock models stay flat** — deterministic test costs must not move on a
  calendar day.
- Comparability across the intro/standard boundary is the *benchmark's* problem
  (Phase 7 can price a usage log under any tier); the meter's job is to match
  the invoice.

**Consequence.** A process that stays alive across a tier boundary keeps its
load-time prices until restarted — acceptable for minutes-long eval runs, and
the price note makes the applied tier auditable per run.

## D-018 — Typed abstention accepts opening *or* closing position (eval run 2b9f39fb diagnosis)

**Status**: accepted · **Date**: 2026-08-06

**Context.** The finalizer recognized `ABSTAIN: <reason>` on the first line
only, while every abstention eval prompt simultaneously imposes the category's
output contract — "End your reply with a line exactly `ANSWER: …`" (that trap
is the point of the category, D-015). The agent system prompts say "reply with
first line exactly `ABSTAIN:`"; the eval prompt says the reply *ends* with an
answer line. A model resolving that tension by closing with the typed
abstention was scored `did_not_abstain` on a reply that invented nothing.
Phase 4's live smoke passed because its prompt carried no format suffix — the
model naturally led with `ABSTAIN:`. Run 2b9f39fb scored abstention 1/10.

**Decisions.**

- **`_abstained` accepts the typed abstention in every shape the conflict
  produces**: an `ABSTAIN:` line opening the reply, closing it, displaced to
  second-to-last by the contract's mandatory final `ANSWER:` line, or jammed
  into that line's payload (`ANSWER: ABSTAIN: …`). An `ABSTAIN:` buried
  mid-reasoning still is not the protocol (guards against "if I could not
  verify I would say ABSTAIN:" false positives).
- **Agent prompts unchanged** ("first line exactly" remains the instruction);
  the suite is untouched, so `suite_hash 6eef41c6706f309a` results stay
  comparable and the hallucination trap stays armed — an invented
  `ANSWER: 18%` fails exactly as before.
- The finalizer is shared by platform agents and both baselines
  (`finalize_cited`), so the tolerance applies uniformly across tracks.

**Consequence — trace adjudication (run 2b9f39fb `results.jsonl`, all 10
abstention questions).** All nine failures were protocol artifacts; zero were
hallucinations. Every failing reply contained an explicit, correct typed
`ABSTAIN:` — seven displaced it to just above a placeholder final answer line
(`ANSWER: N/A%`, `ANSWER: $0`, `ANSWER: NO`, `ANSWER: 0`, `ANSWER: N/A`), two
jammed it into the answer line itself; the one pass (abstention-008) led with
`ABSTAIN:` on line one. No reply asserted a concrete invented value. A test
pins the observed shapes verbatim; agent-side behavior on this category needs
no change.

## D-019 — Budget gate reads committed spend; projections are loop-scale (eval run 2b9f39fb diagnosis)

**Status**: accepted · **Date**: 2026-08-06

**Context.** Run 2b9f39fb crossed its $15 budget by its own meter and still
scored all 133 questions — the §5.4 "hard stop" never fired. The gate read only
*landed* cost, updated after a question completes; with concurrency 4 and
latency spread p50 15s / p95 115s, slow expensive multi_step questions held
their cost invisibly in flight while the cheap tail sailed through the check
(the shipped test used concurrency 1, where the race cannot exist). The run's
artifacts confirm it empirically: the seven long reconciler runs ($7.12
combined, 217-272s each) landed last, and cumulative landed spend first
exceeded $15 at result 132 of 133 — the landed-only gate could never have
fired. Separately,
the pre-run projection said $6.86 where the meter recorded $16.74 at identical
prices: `_PROJECTION`'s per-agent numbers were single-round-trip guesses, but
an agent resends its whole growing context every iteration — the projection
missed the loop, 2.4× under.

**Decisions.**

- **The gate reads committed spend** — landed cost plus a per-question
  projected reservation held while each question is in flight
  (`project_question_cost`, also the §5.4 forecast unit). First skip prints a
  hard-stop notice; a finishing run that overshot prints a warning naming the
  reservation shortfall. Overshoot is now bounded by the in-flight questions'
  projection error instead of unbounded.
- **`_PROJECTION` constants are whole-loop totals**, calibrated from the run's
  per-question costs (judge-subtracted per-agent means → counsel 14k/1.8k,
  analyst 4.5k/750, reconciler 87k/12.7k, judge 3k/450; suite total $16.90 vs
  $16.74 metered). The miss was almost entirely the reconciler — real mean
  $0.45/question vs the $0.0855 single-round-trip guess; the analyst guess was
  actually high. The reconciler mean is cap-censored (6 of its 22 runs hit the
  per-run budget cap), so it floors reconciler-heavy projections; per-run caps
  bound each question's actual spend regardless.
- **A regression test runs concurrency 4 with a deliberately slow expensive
  question** and asserts the stop trips while its cost is in flight (verified
  failing against the pre-fix gate).

**Consequence.** A hard *guarantee* against overshoot is impossible without
killing in-flight LLM calls (their tokens are already bought); bounding by
reservation error is the honest contract, and calibrated projections keep that
bound tight. Under-projection now biases the gate *closed* earlier, never open.

## D-020 — Per-run budgets are sized empirically; the Reconciler cap is $2.50 (eval run 127c5ad8)

**Status**: accepted · **Date**: 2026-08-06

**Context.** The post-diagnosis category re-run (run 127c5ad8,
`--categories abstention,multi_step`, correct D-017 prices) split cleanly:
abstention scored 100.0 — the D-018 finalizer fix holds — while all six
Reconciler multi_step questions exhausted again, this time at the *correct*
$1.00 real-money cap (~$1.00 metered per run, `submitted: 0`, p95 393s). With
the 1.5× meter error gone, the ambiguity is gone too: the workflow legitimately
costs more than its budget. That budget was `run_budget_usd × 2` (D-014's
"workflow headroom") — a proportion guessed in Phase 4, before any live run
existed. Both live runs show the same censored signature: every exhausted run
finished the expensive middle of the workflow (ingest → match → scan →
allocations) and died before `submit_batch`, so the cap converts ~$1.00 of real
spend per question into a dead run and an empty answer. A cap below the
workflow's true cost doesn't bound waste — it guarantees it.

**Decisions.**

- **A per-run budget must be sized empirically against the measured cost of
  the workflow it guards.** The original $1.00 predated any live run;
  multiplying the Q&A knob was a shape of guess, not a measurement — Counsel's
  budget says nothing about what an ingest→match→scan→allocate→submit loop
  costs. A budget ships as a hypothesis only until a live run prices the
  workflow; after that, the measurement wins.
- **The Reconciler cap is $2.50.** Censoring bounds the true cost only from
  below ($1.00+), but every exhausted run lacked only submit + wrap-up, so
  2.5× the censoring point covers the observed spend plus the bounded
  remainder with margin. Implemented in `_limits_for` as
  `max(run_budget_usd, $2.50)` — the env knob can raise the cap above the
  floor, never shrink it — and iteration headroom stays 2× (iterations were
  never the binding constraint; budget was). Counsel/Analyst stay on
  `run_budget_usd` unchanged.
- The config test pins the split: Reconciler at exactly $2.50 under the
  default $0.50 base (and never below the floor), Counsel/Analyst at the
  settings value.

**Consequence.** The cap stops censoring at $1.00, so the D-019 `_PROJECTION`
reconciler mean (calibrated on capped runs) now under-floors reconciler
questions that will run to completion — per-question spend may legally reach
$2.50, and the committed-spend gate still bounds suite overshoot by
reservation error. The first uncensored run's artifacts are the recalibration
source, and the check on $2.50 itself. No live run, baseline write, or
projection recalibration happened with this change; the multi_step re-run
stays on the operator's list.

## D-021 — Truncated replies are never acted on; the Reconciler output ceiling is 16384 (eval run ddb797dc)

**Status**: accepted · **Date**: 2026-08-06

**Context.** Run ddb797dc (post-D-020, $2.50 Reconciler cap) re-failed all six
multi_step reconciler questions — 5/6 `run_exhausted` at only $1.20–1.40 with
372–535s latencies, 1/6 (`multi_step-003`) `no_answer_line` at $0.22. Budget
was no longer the binder; the iteration cap was. Every trace shows one clean
`scan_anomalies`, one clean `compute_allocations`, `submitted: 0`. The cause
is arithmetic, not judgment: the seeded world pays **95–103 artists in every
full period**, and `compute_allocations` instructs the agent to feed those
rows to `submit_batch` verbatim — ≈13k chars of allocations JSON (~3.2k tokens
at the repo's len/4 floor, more under the real tokenizer) *before* flags with
evidence payloads, the reviewer note, tool-call encoding, and preamble text.
`AgentSpec.max_tokens` was 4096: **a contract-faithful full-period
`submit_batch` call cannot stream inside the output window** — deterministic,
which is why the category failed 6/6 in all three runs while scan-only
reconciliation questions (no submit) passed. The runtime then made a hard wall
into a loop: it ignored `stop_reason`. The SDK assembles streamed tool
arguments from partial JSON, so a `max_tokens`-cut `submit_batch` arrives as a
*prefix dict* — usually failing Pydantic validation (`invalid_tool_args`
denial → retry → identical truncation → iteration cap), and in the worst case
*validating* with a silently missing tail of allocations, which only luck kept
from submitting a partial batch. A reply cut mid-text (no tool call started)
took the other branch: empty `tool_calls` reads as termination, so the
truncated prose **finalized as the answer** — `multi_step-003`'s early
"completed" with no `ANSWER:` line. (Its `information_schema` probe, denied by
the SQL policy, was mid-loop flailing — symptom, not cause.)

**Decisions.**

- **A `max_tokens`-truncated reply is never acted on.** Tool calls from a
  truncated reply are discarded un-executed — keyed off `stop_reason`, not off
  validation failing, because a prefix of streamed arguments can validate and
  still not be what the model said. Each discarded call gets an explicit
  `is_error` tool result telling the model it was cut off and to re-issue.
  Truncated text never finalizes: the partial stays in history and a runtime
  notice asks the model to continue and finish. Both paths record an
  `output_truncated` guardrail incident (a span, per invariant 6) and consume
  their iteration. No `tool_call` span is emitted for a discarded call — it
  was never a validated attempt — so T2 counts (`single_batch`, `no_batch`)
  keep meaning "real calls"; the guardrail span carries the tool names.
- **The Reconciler's `max_tokens` is 16384** (other agents stay at 4096): the
  measured worst case needs ~4.5–6k real output tokens for the verbatim
  allocation list + flags + note; 16384 holds it with ~3× margin, and the
  $2.50 budget still bounds actual spend. This is the unblocking fix; the
  truncation contract is the safety fix that outlives it.
- **Correction to D-020's aside**: "iterations were never the binding
  constraint" was an artifact of budget-censored runs — once the cap rose, the
  same truncation loop ran into the iteration cap instead. Caps only name the
  binder, not the disease; the disease was the un-streamable call.
- Considered and deferred: passing allocations by reference (e.g.
  `submit_batch` reading the last compute result server-side) would shrink the
  call to O(exclusions) instead of O(roster) and remove the transcription
  surface entirely — but it changes the D-013 tool contracts and the agent's
  ownership of the submitted list, so it is a design decision for a future
  phase, not a bug fix.

**Consequence.** Runs can no longer "complete" on cut-off text or burn
iterations re-streaming an impossible call; both failure shapes surface as
visible `output_truncated` incidents. The multi_step re-run (operator's list)
is expected to reach `submit_batch` within budget; `_PROJECTION`
recalibration still waits for that first uncensored run per D-020.

## D-022 — Artifact and trace paths anchor at the repo root (eval harness)

**Status**: accepted · **Date**: 2026-08-06

**Context.** Eval artifacts wrote to CWD-relative `data/evals/<run_id>`; a run
launched from inside an old artifact directory nested the new run's output
there (`data/evals/127c5ad8…/data/evals/ddb797dc…`). Trace JSONL and the
ingest inbox resolved `data/` the same fragile way.

**Decision.** Relative configured paths anchor at the repository root, located
from the package (`pyproject.toml` marker walk-up), never from the process
CWD: `repo_root()` / `anchor_path()` in `backline/config.py`, plus
`Settings.data_path` as the absolute form of `data_dir`. Applied to eval
artifact dirs (`evals.runner.artifact_dir`), the trace `JsonlSink`, the B0
corpus index, the ingest inbox, and the embed pipeline. Absolute paths —
compose's `DATA_DIR=/data`, tests' `tmp_path` — pass through untouched, so
deployment configuration stays authoritative and the anchor only replaces the
accidental CWD dependence. Tested with a chdir-into-tmp regression test
mirroring the observed nesting.
