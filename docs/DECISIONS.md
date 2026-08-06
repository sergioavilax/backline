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
