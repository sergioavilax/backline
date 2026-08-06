# Phase Log

Append-only. One entry per phase: what shipped, what was deferred, any deviation + why.

---

## Phase 0 — Repo Skeleton, Compose, CI, Conventions (2026-08-05)

**Shipped**

- Monorepo layout: `backline/` package (`api` with `/healthz` + `/readyz`, `db/migrate`
  raw-SQL migration runner, `config`), `migrations/` (empty `0001_baseline`), `scripts/doctor.py`,
  `tests/`, `ui/`, `docker/`, `docs/`, `.github/workflows/`.
- `pyproject.toml`: Python 3.12, uv-locked, ruff (lint+format), mypy `--strict`, pytest +
  pytest-asyncio. Suite: 5 tests (3 keyless-anywhere, 2 Postgres-backed that skip without
  `DATABASE_URL`).
- Migration runner: `migrations/*.sql` in filename order → `schema_migrations` table, one
  transaction per file, connect-with-retry for the init container. Chosen over Alembic —
  rationale in D-000.
- `ui/`: Next.js 15.5.22 + TypeScript + Tailwind v4, pnpm (lockfile committed),
  `output: "standalone"` for Docker, placeholder page only (surfaces are Phase 6).
- `docker-compose.yml`: `db` (`pgvector/pgvector:pg16`, pg_isready healthcheck), `init`
  one-shot (migrations now; seed/embed append in Phases 1/3), `api` (healthcheck hits
  `/readyz`, so "healthy" means DB-wired), `ui`. Every variable has a default — bare
  `docker compose up` needs no `.env`.
- `Makefile`: `up/down/logs/ps/test/lint/typecheck/fmt/doctor` real; `seed`, `embed`,
  `eval-smoke`, `corpus-tokens` are phase-gated stubs that fail loudly with their phase number.
- `make doctor` (stdlib-only): docker daemon + compose, port availability (5432/8000/3000),
  env-file presence, `.gitattributes` LF enforcement, `core.autocrlf`, WSL `/mnt/c` trap.
- CI (`.github/workflows/ci.yml`): `lint-type`, `test` (pgvector service container),
  `ui` (pnpm lint + build), `eval-regression` stub gated on `ANTHROPIC_API_KEY` at step
  level so forks/keyless PRs stay green.
- Docs: `CLAUDE.md` (invariants + session protocol + conventions), `docs/DECISIONS.md`
  (D-000 stack rationale), `docs/TRACEABILITY.md` (seeded from BUILD_PLAN §1, with status
  column), README stub (real README is Phase 8). `.gitattributes` forces LF; `.env.example`
  annotated.

**Verified**

- `ruff check` / `ruff format --check` / `mypy --strict` / `pytest` all green.
- Postgres-backed tests executed against a real Postgres 16 instance (5 passed, 0 skipped);
  migration runner CLI confirmed idempotent (second run applies nothing).
- Uvicorn boot smoke: `/healthz` 200; `/readyz` 200 against live DB, 503 with DB down.
- `pnpm lint` + `pnpm build` green; standalone server bundle produced.
- `docker compose config` valid; `make doctor` exits 0.

**Deviations / notes**

- **Full `make up` cold boot was not executed in the build sandbox**: the sandbox's egress
  policy blocks Docker Hub image pulls (daemon started fine; blob CDN denied). The compose
  file is config-validated and both Dockerfiles mirror the exact command sequences run
  locally (`uv sync --frozen` / `pnpm build`). Cold-boot DoD check falls to the dev
  machine + CI service containers.
- create-next-app's Google-hosted fonts (`next/font/google`) were replaced with system
  fonts: a build-time network fetch would make Docker builds non-hermetic. Phase 6 selects
  the real faces (Inter Tight / IBM Plex Mono) per `docs/UI_DIRECTION.md`, self-hosted.
- `doctor` treats a missing `.env` as a warning, not a failure — compose ships working
  defaults for a keyless boot, so `.env` is genuinely optional.

**Deferred**: nothing from the Phase 0 scope.

---

## Phase 1 — Synthetic World + Answer Key (2026-08-05)

**Shipped**

- `backline/royaltycalc/` — the single royalty-math implementation (D-001), built
  test-first, stdlib-only: `rounding.py` (the one policy: 6dp half-even line-level,
  cents half-even at final aggregation), FX, rate cards with territory fallback +
  carve-outs, escalators (period-start semantics, D-003), amendment supersession over
  canonical terms JSON, recoupment waterfall with per-account pooling
  (cross-collateralization = shared account, D-004) and minimum-guarantee top-ups.
  99 unit tests incl. Hypothesis property suites for the plan's two invariants;
  **100% branch coverage** (CI gates at ≥95%).
- `migrations/0002_world_schemas.sql` — all §3.3 tables across `label` / `staging` /
  `truth` / `app` (28 tables), NUMERIC(18,6)/(18,8) money, statement-line indexes,
  borderline-aware `truth.anomaly_registry.expected_flag_kind NULL`.
- `datagen/` — the deterministic Foldback Records universe from `WORLD_SEED` via named
  SeedSequence streams (world stream + per-period streams; `rng.py` is the only RNG
  construction site, grep-enforced). World: 150 artists (12 cross-collateralized),
  **301 base + 84 amendment contracts** (canonical JSON terms + numbered-clause PDFs +
  txt sidecars, ReportLab invariant mode), 549 releases / 2,366 tracks / 30
  compilations, 6 feeds with 6 CSV dialects, **468,160 statement lines** across 12
  periods (≥450K floor), 88K dashboard rows, 100 advances + 80 expenses, fixed monthly
  FX. Edge cases seeded: JP carve-out (artist 130), mid-year termination with post-term
  accounting (artist 119), $1,200 minimum guarantee (artist 110), injection canary in
  one contract PDF (FBR-C-00670, corpus-only — never in terms JSON).
- Truth engine: `truth.expected_ledger` for all 150 × 12 artist-periods, computed from
  the *clean* line set through `royaltycalc` (anomalies are reporting corruption —
  D-005). 40 registered anomalies: ≥3 of each of the 7 kinds + 2 borderline
  (`expected_flag_kind IS NULL`).
- CLI: `datagen seed` (`--if-empty` for compose init), `datagen emit-period 2026-07`
  (fresh dialect drops + `status=received` rows, idempotent, refuses seeded window),
  `datagen fingerprint [--from-db --files]`, `datagen corpus-tokens`. Make targets
  `seed` / `emit-period` / `corpus-tokens` now real; compose `init` runs
  migrate + seed with a shared `/data` volume.
- Determinism (D-006): committed golden `tests/golden/world_fingerprint.json` (17 table
  hashes + 842 file hashes). Tests pin in-memory build == golden, Postgres content ==
  golden, and rendered bytes == golden. 60 new tests (38 datagen + 22 royaltycalc
  additions); Postgres-backed ones skip keyless as before.

**Verified**

- `make lint` / `make typecheck` (mypy --strict, 50 files) / full pytest green.
- Full `make seed` on a real Postgres 16: **~30s end-to-end** (build 12s, 385 PDFs 3s,
  COPY load of 571,776 rows 11s) — comfortably under the 3-minute DoD.
- Seed twice → identical fingerprints including every PDF/CSV byte; DB-derived
  fingerprint == in-memory fingerprint == committed golden.
- `emit-period 2026-07`: 41,117 lines across 6 drops, byte-identical on re-run, 0 rows
  double-inserted, refuses in-window periods.
- Hand audit: 5 artist-period calculations verified digit-for-digit in
  `docs/WORLD_AUDIT.md` (simple / advance-recoup / un-pooled sync windfall + escalator
  crossing / cross-collateral flip / minimum guarantee).
- `make corpus-tokens` (DoD paste; sandbox had no egress for the tiktoken encoding
  download, so the labeled bytes/4 estimate path ran — with egress the same command
  reports exact o200k_base counts):

  ```
  corpus tokens (estimate:bytes/4 (tiktoken encoding unavailable offline))
    contracts:    385 files       1.1 MiB       288,905 tokens
    statements:    72 files      27.1 MiB     7,104,659 tokens
    total:         7,393,564 tokens  = 37.0x a 200,000-token context window
  ```

**Deviations / notes**

- Contract counts landed at 301 base + 84 amendments against the plan's "~320 + ~90"
  (within its ~tolerance; tests assert 280–360 / 70–110). Artist-deal timelines are
  organic (join dates × deal durations truncated at the window), so forcing exact
  totals would have meant synthetic-looking timelines.
- `truth.qa_answer_key` is created empty — BUILD_PLAN populates it in Phase 5
  (`evals/generate_suite.py`).
- JPY feed amounts are whole-yen at *generation* (the feed's own reporting habit,
  applied once in datagen); royalty math consumes reported values unchanged through
  `money6`, so the one-rounding-policy invariant is untouched.
- The 5-line spot-audit deliberately over-delivers: two of the five walkthroughs
  (Audits 1 and 5) are exact to all six decimal places for *every* intermediate step.

**Deferred**: nothing from the Phase 1 scope.

---

## Phase 2 — Providers, Runtime, Tracing, Guardrail Frame (2026-08-06)

**Shipped**

- `backline/providers/` — the provider abstraction (§4.1), the only gate for LLM calls:
  - Provider-neutral wire types (`Message`/`ToolCall`/`ToolSpec`/`CompletionRequest`/
    `CompletionResult`, five normalized stop reasons) in `base.py`.
  - **AnthropicProvider** on the official SDK (D-007): always-streaming Messages API
    with tool use; SDK-delegated `anthropic-version` pinning, jittered-backoff retries
    (429/529/5xx/connect), and `input_json_delta` assembly; consecutive tool results
    merge into one user turn; unit-tested against canned SSE via `httpx.MockTransport`
    including an argument split mid-`\u` escape.
  - **OpenAICompatProvider** over httpx for any OpenAI-format endpoint (vLLM/OpenAI):
    JSON-string tool-argument parsing (unparseable → loud `ProviderError`),
    finish-reason mapping, own jittered retry/backoff (entropy from `secrets`;
    sleeper/jitter injectable so tests run instantly).
  - **MockProvider** — ordered script of `MockTurn`s with per-turn `match` guards on
    the rendered request; a drifted scenario fails at the exact divergent turn.
  - **Registry** `config/models.yaml` → `ModelRegistry` (D-007): current Anthropic
    tiers (opus-5 $5/$25, sonnet-5 $3/$15, haiku-4-5 $1/$5), `local-qwen` at $0, and
    mock models priced like real tiers; quoted-string prices → exact `Decimal`, bare
    floats rejected at load.
- `backline/core/` — the platform heart (§4.2/§4.5–4.7):
  - `trace.py` — span tree `run → iteration → {llm_call|tool_call|guardrail|compression}`
    with OTel-shaped `gen_ai.*` attrs; sinks: Postgres (`app.runs`/`app.spans`,
    insert-on-start/complete-on-end — D-008), JSONL per run under `data/traces/`,
    in-proc `TracePubSub` for the Phase 6 SSE feed, in-memory for tests.
  - `costmeter.py` — Decimal cost from registry prices through `money6` (one rounding
    policy, invariant 1); per-call records for the Phase 7 benchmark.
  - `guardrails.py` — the frame (§4.6): `RunLimits` (iteration/budget/tool-timeout/
    result-size, env-configurable), Pydantic tool-arg validation, unknown-tool denial,
    and a `ToolCheck` registration point for Phase 3's SQL policy / Phase 4's injection
    flagging. Every denial is an `Incident` recorded as a `guardrail` span.
  - `memory.py` — `SessionMemory` (rolling window, overflow folds through a pluggable
    summarizer into a running `<conversation_summary>`; deterministic elision note when
    keyless) and `WorkingMemory` (per-run tool-result dedup by `(tool, content)` hash).
  - `runtime.py` — `AgentRuntime` loop with typed `FinalAnswer(answer, citations[],
    abstained)` termination, generic Pydantic-typed `Tool[P]` bindings, per-agent
    `AgentSpec` (model policy incl. `utility_model`), tool timeout → `is_error` result,
    oversize-result compression via utility model (or truncation) as `compression`
    spans, budget/iteration exhaustion → `status=exhausted` with run-level guardrail
    span (D-009). Limit trips are never silent truncations.
- `scripts/dev_run.py` — scripted mock agent end-to-end: three-turn conversation over
  two toy tools, printed span tree with tokens/cost, JSONL trace; `--postgres` also
  persists to `app.runs`/`app.spans`. Covered by a keyless subprocess test.
- Config: `Settings` budgets are now `Decimal` (money is never float) + new
  `MAX_ITERATIONS`/`TOOL_TIMEOUT_S`/`MAX_RESULT_TOKENS`/`OPENAI_COMPAT_API_KEY` env
  knobs (`.env.example` annotated); `jsonutil` (the one encoder) additionally
  serializes UUID + datetime for trace records.
- Tests: 61 new (203 total; 2 more deselected by default) — providers (registry, mock,
  Anthropic SSE/retry/error paths, OpenAI-compat normalization/retry), core (costmeter
  exactness + half-even quantization, guardrails, memory, tracer shape/pubsub/JSONL,
  14 runtime-loop scenarios), dev_run subprocess, Postgres span-tree integration.
  Live verification ships as `pytest -m live` (two tests: text + forced tool-use round
  trip on `claude-haiku-4-5`), excluded by default via `addopts`.

**Verified**

- `make lint` / `make typecheck` (mypy --strict, 77 files) / full `make test` green:
  **203 passed, 2 deselected** with `DATABASE_URL` set against a real Postgres 16
  (includes all Phase 1 world/fingerprint integration tests — world untouched).
- Phase 2 DoD: zero-network unit+integration tests (Anthropic provider driven through
  `httpx.MockTransport` canned SSE; no test needs a key) ✅ · mock run produces the
  asserted span tree **in Postgres** (parentage, kinds, JSONB attrs, NUMERIC cost as
  Decimal `0.001620`) ✅ · budget exhaustion path ends `status=exhausted` with
  `guardrail` span ✅ (iteration-cap path too) · live-API marker ships skipped-by-default
  for the one-time human run ✅.
- `uv run python scripts/dev_run.py`: 3 iterations, 8 spans, `$0.002430` metered,
  span tree printed, JSONL written; `--postgres` variant persisted and was inspected
  row-by-row.
- The Postgres integration test caught a real bug pre-commit (span insert-on-end broke
  the `parent_id` self-FK) — fixed by insert-on-start/complete-on-end, recorded as
  D-008.

**Live verification (manual, 2026-08-05)** — the DoD's one-time human `pytest -m live`
run against the real Anthropic API, executed by the maintainer on the dev machine. Both
live tests passed (plain text completion + forced tool-use round trip on
`claude-haiku-4-5`):

```text
=================================================== test session starts ====================================================
platform linux -- Python 3.12.13, pytest-9.1.1, pluggy-1.6.0 -- /home/somx/code/backline/.venv/bin/python
cachedir: .pytest_cache
hypothesis profile 'default'
rootdir: /home/somx/code/backline
configfile: pyproject.toml
testpaths: tests
plugins: hypothesis-6.165.2, anyio-4.14.2, asyncio-1.4.0, cov-7.1.0
asyncio: mode=Mode.AUTO, debug=False, asyncio_default_fixture_loop_scope=None, asyncio_default_test_loop_scope=function
collected 205 items / 203 deselected / 2 selected

tests/providers/test_live_anthropic.py::test_live_text_completion PASSED                                             [ 50%]
tests/providers/test_live_anthropic.py::test_live_tool_use_round_trip PASSED                                         [100%]

===================================================== warnings summary =====================================================
.venv/lib/python3.12/site-packages/fastapi/testclient.py:1
  /home/somx/code/backline/.venv/lib/python3.12/site-packages/fastapi/testclient.py:1: StarletteDeprecationWarning: Using `httpx` with `starlette.testclient` is deprecated; install `httpx2` instead.
    from starlette.testclient import TestClient as TestClient  # noqa

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
======================================= 2 passed, 203 deselected, 1 warning in 4.19s =======================================
```

**Deviations / notes**

- **Live Anthropic verification not executed in the build session** — no API key in the
  build environment, by design (the DoD assigns it to the human as a one-time manual
  `pytest -m live` run; results belong in this log when run — now recorded above,
  2026-08-05). The provider's wire behavior is pinned offline against canned SSE instead.
- The live smoke targets `claude-haiku-4-5` rather than an Opus-class model: it
  verifies plumbing, not capability, and §10's budget discipline names Haiku-class the
  cheap tier for dev pokes.
- `RunLimits` defaults live in code with env overrides (`RunLimits.from_settings()`);
  per-agent overrides arrive with the Phase 4 agent configs.
- Phase 2's §4.2 "assemble context" step is deliberately minimal (session context +
  per-run transcript + dedup); entity-keyed note recall joins in Phase 3/4 with the
  notes tools.

**Deferred**: nothing from the Phase 2 scope.

---

## Phase 3 — Tools + RAG (2026-08-06)

**Shipped**

- `backline/tools/sqlpolicy.py` — the parser-level SQL policy (invariant 3's
  enforcement point): sqlglot parse → exactly one SELECT/set-op, default-deny schema
  allowlist `{label, staging}` (truth/app/rag/pg_catalog dead by construction, with the
  named canary test pinning `truth`/`app` out of `ALLOWED_SCHEMAS`), every table
  schema-qualified (CTE names resolved), no DML/DDL/multi-statement/`SELECT INTO`/
  `FOR UPDATE`, side-effectful-function denylist (pg_sleep/pg_read_*/dblink/sequence
  ops/...), LIMIT 200 injected when absent and capped when larger. Doubles as a
  guardrails `ToolCheck`, so a denied query is a `guardrail` span, not a log line.
- `backline/tools/sqltool.py` — `sql_query`: policy → `EXPLAIN (FORMAT JSON)` cost
  ceiling (`SQL_COST_CEILING`, rejects pathological joins pre-execution) → READ ONLY
  transaction + server-side statement timeout → aligned text table with row count and
  disclosed LIMIT rewrites. Decimal-exact rendering.
- `migrations/0003_rag_and_ingestion.sql` — `rag.contract_chunks` (clause chunks,
  weighted generated tsvector, `vector(384)`, per-row `embedding_model` +
  `content_hash`; ivfflat deliberately left to the embed job per §9) and
  `staging.ingested_lines` (the agent ingestion target, D-010).
- `backline/rag/` — the §4.4 pipeline (D-002):
  - `chunker.py` — clause-aware chunks from the renderer's deterministic `.txt`
    sidecars (chunks *are* clauses; §-heading split; oversize clauses part-split on
    paragraphs — the seeded corpus needs none). Injection canary chunks verbatim
    (FBR-C-00670 §7), pinned by test.
  - `embedder.py` / `reranker.py` — bge-small-en-v1.5 + ms-marco cross-encoder via the
    optional `embed` extra, with deterministic offline twins (`hash-bow-384-v1`
    feature-hashed BoW embedder; `lexical-overlap-v1` reranker) used by tests, keyless
    CI, and model-less environments (D-011).
  - `governing.py` — the structured-first governing-document filter: SQL resolves
    bases + effective amendments as of a date; superseded sections excluded at clause
    granularity (`royalties→§3` etc., mapping pinned against the renderer).
  - `search.py` — governing filter → FTS (`ts_rank_cd`) + pgvector cosine
    (ivfflat.probes=16) → RRF (k=60, 50/leg) → cross-encoder rerank on the fused
    top-30 only, `RERANK`-toggleable; single-model store enforced (query/store
    embedder mismatch raises); zero-embedding stores degrade to recorded `fts-only`.
  - `embed.py` — `make embed`: reconcile chunks by content hash (unchanged rows
    untouched, changed re-embed, stale deleted) → embed missing/model-changed rows →
    ivfflat build + ANALYZE after bulk (retrained when vectors changed). Idempotent;
    `--best-effort` (compose init) builds chunks even when no model can load.
- `backline/tools/` — the rest of the §4.3 matrix, all Pydantic-typed `Tool` bindings:
  - `retrieval.py` — `search_contracts` (structural citations `FBR-C-00501 §3`,
    artist/as-of/history params, graceful unknown-artist outcome) and `read_clause`
    (verbatim clause fetch; misses list what exists).
  - `calc.py` + `ledger.py` — `calc_royalties` (D-012): ledger mode rebuilds the full
    engine input from Postgres (attribution by ISRC/UPC + era-by-origin-date, terms
    via `resolve_terms` per period, advances/expenses as charges, opening balances,
    FX) and runs the one royalty engine over reported lines minus agent exclusions
    (structurally invalid lines auto-excluded and reported); spot mode rates
    hypothetical rows under governing terms with true escalator state, labeled
    pre-recoupment.
  - `normalizer.py` + `statements.py` — the Reconciler chain: `ingest_statement`
    (all 6 CSV dialects parsed back to canonical values, datagen's own `line_hash`
    recomputed, staged into `staging.ingested_lines` only — statements stay
    `received`; parse report with per-currency totals + duplicate/negative/off-period
    signals), `match_lines` (ISRC/UPC catalog partition over label or staged lines),
    `submit_batch` (the one write path toward money: `proposed` batch + allocations +
    flags, run-stamped; no approval path exists in any tool).
  - `notes.py` — `save_note`/`recall_notes` on `app.notes`, entity-ref validated,
    run-stamped. `artists.py` — shared exact-then-fuzzy artist resolution with
    candidate suggestions.
- `backline/core/runcontext.py` — ambient run id (`ContextVar`) set by the runtime
  around each run; how gated writes stamp `submitted_by_run`/`created_by` without
  widening the tool-handler signature.
- `evals/retrieval_probe.py` (+ `make retrieval-probe`) — 40 deterministic clause-lookup
  queries with structurally resolved golds, run through the real pipeline in
  {scoped, unscoped} × {rerank, fused}; prints MRR/recall@k + the rerank lift.
- Wiring: `make embed` real; compose `init` now runs migrate → seed → embed
  (--best-effort); `emit-period` additionally records its month's FX rows (D-012);
  new settings `EMBED_MODEL`/`RERANK_MODEL`/`SQL_ROW_LIMIT`/`SQL_COST_CEILING`
  (.env.example annotated); `sqlglot` added as a core dependency,
  `sentence-transformers` as the optional `embed` extra.

**Verified**

- `make lint` / `make typecheck` (mypy --strict, 115 files) green; full suite with
  `DATABASE_URL` against Postgres 16 + pgvector: **343 passed, 2 deselected** (140 new
  tests; includes all Phase 1/2 suites — world fingerprint untouched).
- **The answer key reproduced from the DB**: for all **130 of 150** artists untouched
  by line-level anomalies, ledger mode matches `truth.expected_ledger` *exactly* —
  gross, recouped, net_payable, balance_after, microdollar precision, full 12-period
  chain (D-001 proven end-to-end from Postgres). A sensitivity canary asserts
  corrupted artists diverge; excluding the registry's injected lines reconciles
  dup/bleed/negative/spike artists back to the key.
- Normalizer round-trip: for every one of the 6 dialects, parsing the rendered
  2026-02 drop reproduces the DB's canonical values and datagen's stored line hashes.
- Reconciler flow on a fresh month: `emit-period 2026-07` → ingest stages 5K+ lines
  (label untouched, status stays `received`, re-ingest replaces), match surfaces the
  injected unknown-ISRCs, staged lines flow through `calc_royalties(include_staged)`,
  `submit_batch` lands `proposed` with the run stamp.
- Embed job: 385 contracts → **2,961 clause chunks**; second run touches nothing;
  a tampered chunk is restored and re-embedded alone; ivfflat present after build.
- Retrieval probe (offline deterministic stack — hash-bow embedder +
  lexical-overlap reranker; see deviation below):

  ```
  retrieval probe — 40 clause-lookup queries, embedder=hash-bow-384-v1,
  reranker=lexical-overlap-v1, as_of=2026-06-30

  mode                  MRR R@1  R@3  R@5  R@10
  scoped/fused        0.251 0.10 0.23 0.45 0.72
  scoped/rerank       0.387 0.20 0.50 0.62 0.85
  unscoped/fused      0.006 0.00 0.00 0.00 0.05
  unscoped/rerank     0.006 0.00 0.00 0.00 0.05

  rerank lift (scoped MRR): 0.251 → 0.387 (+0.136)
  ```

  The rerank stage lifts scoped MRR by **+0.136** (R@3 0.23→0.50) even on the lexical
  stack. The unscoped collapse is the design argument in numbers: with the artist only
  *named in the text*, lexical retrieval can't find the governing clause — entity and
  governance scoping belong in structure (D-002), not in the embedding.
- End-to-end mock-agent run (DoD): one scripted `AgentRuntime` run calls all nine
  tools in sequence against the seeded world — the adversarial `truth` query dies as
  a `sql_policy` guardrail span while the run recovers and completes; every other
  tool executes cleanly; staging writes + note are stamped with the traced run id;
  span tree asserted from both the in-memory sink and Postgres.

**Real-model retrieval probe (manual, 2026-08-06)** — the deviation below noted the
build sandbox could not load the real models; `make retrieval-probe` has now been run
by the maintainer on the dev machine (bge-small-en-v1.5 + ms-marco cross-encoder via
`uv sync --extra embed`):

```text
retrieval probe — 40 clause-lookup queries, embedder=(store default), reranker=cross-encoder/ms-marco-MiniLM-L-6-v2, as_of=2026-06-30
mode                  MRR R@1  R@3  R@5  R@10
scoped/fused        0.298 0.07 0.33 0.65 0.95
scoped/rerank       0.398 0.15 0.57 0.78 0.97
unscoped/fused      0.000 0.00 0.00 0.00 0.00
unscoped/rerank     0.003 0.00 0.00 0.00 0.03
rerank lift (scoped MRR): 0.298 → 0.398 (+0.100)
```

(`embedder=(store default)` is the probe's label for "no override": queries embed with
the model the chunk store records, i.e. `BAAI/bge-small-en-v1.5`.)

The real stack confirms the offline picture and sharpens it:

- **The rerank lift replicates on production models**: scoped MRR 0.298 → 0.398
  (+0.100), R@3 0.33 → 0.57, R@10 reaching 0.97 — same shape as the offline stack's
  +0.136, now measured with the cross-encoder the runtime actually ships.
- **The unscoped collapse is total — strong evidence for D-002**: with real semantic
  embeddings and the governing-document filter off (artist named only in the query
  text), the probe scores MRR 0.000 fused / 0.003 reranked. On a corpus of ~385
  near-identical generated contracts, every §3 reads like every other §3 — semantic
  similarity cannot single out *whose* clause governs, and the cross-encoder can only
  reorder candidates retrieval already surfaced. Retrieval without the
  governing-document filter doesn't degrade on this corpus; it fails entirely.
  Entity/governance scoping belongs in structure (the SQL filter), exactly as D-002
  argues.

**Deviations / notes**

- **Real-model retrieval numbers not produced in this session**: the build sandbox's
  egress policy blocks HuggingFace downloads (bge-small / ms-marco cannot load), so
  the probe ran on the deterministic offline stack and those are the numbers recorded
  above — clearly labeled, mechanism identical (same precedent as Phase 0's compose
  cold-boot and Phase 1's tiktoken estimate). `make retrieval-probe` on the dev
  machine (with `uv sync --extra embed`) produces the bge + cross-encoder numbers;
  they belong in this log when run.
- The Docker image ships without the `embed` extra for now — PyPI's linux torch wheels
  are CUDA builds (~5 GB); compose init runs `embed --best-effort` (chunks + FTS-only
  search everywhere, full hybrid after a host-side `make embed`). Rationale + the
  CPU-wheel re-lock follow-up recorded in D-011.
- §4.3's "staged raw lines" is interpreted strictly per invariant 5: a new
  `staging.ingested_lines` table, statements stay `received` until batch approval
  promotes (Phase 6 review action) — D-010.
- `emit-period` gained an FX-row insert for its month (runtime calculator needs it for
  staged-period math); seeded content and the golden fingerprint are untouched.
- Entity-keyed *auto*-recall of notes into agent context (the §4.5 scope-3 tail) rides
  with the Phase 4 router as planned; the durable notes tools shipped here.

**Deferred**: nothing from the Phase 3 scope.
