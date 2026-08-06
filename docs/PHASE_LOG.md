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

---

## Phase 4 — The Three Agents + Router (2026-08-06)

**Pre-tasks (maintainer-requested, before the phase)**

1. **Real-model retrieval probe recorded** — the maintainer's dev-machine run of
   `make retrieval-probe` (bge-small + ms-marco) appended to the Phase 3 entry
   above, with the unscoped-zero result called out as direct evidence for D-002.
2. **D-011 CPU-wheel re-lock: staged, environment-blocked.** This sandbox's egress
   gateway denies `download.pytorch.org` outright (CONNECT 403 via proxy *and*
   `403 host_not_allowed` direct — both captured), so `uv lock` against the
   pytorch-cpu index cannot run here, and Docker Hub's blob CDN is equally denied
   (`production.cloudfront.docker.com` 403), so no base image pull → no image
   build either. What shipped instead: `torch>=2.2` is now an explicit member of
   the `embed` extra (locked from PyPI — metadata-only change) and the exact
   `[tool.uv.sources]`/`[[tool.uv.index]]` block is staged **commented out** in
   `pyproject.toml` (uv accepted the syntax before hitting the network; an active
   pin the environment can't fetch would break every implicit `uv lock`, including
   `uv run`). Dev-machine procedure, also in the Dockerfile comment: uncomment the
   block → `uv lock` → add `--extra embed` to both `uv sync` lines in
   `docker/api.Dockerfile` → `docker compose build` to verify.
3. **Per-process model cache (perf bug from the probe run).** `search_chunks`
   resolved the store's embedding model with a fresh `build_embedder` per query —
   with real models, a weights load per call (~160 loads per probe run, and one
   per `search_contracts` call at runtime). `get_embedder`/`get_reranker` are now
   `lru_cache`d process-wide accessors used by the search path, the retrieval
   tools, the embed job, and the probe; `build_*` stay uncached for explicit
   construction. Pinned by tests (cache identity; one-miss-many-hits through
   repeated `search_chunks`). The `get_sentence_embedding_dimension`
   FutureWarning (renamed in sentence-transformers ≥ 5.6): the dim check now
   probes for `get_embedding_dimension` and falls back for older releases.

**Shipped**

- `backline/agents/` — the three agents as configuration of one runtime (§2):
  - `prompts/{counsel,analyst,reconciler,router}.md` — versioned prompt files
    loaded verbatim as system prompts; `promptfiles.py` content-hashes each and
    `AgentSpec.trace_attrs` carries `prompt_sha256` into run meta (new, additive
    runtime field), so every run and future eval result pins to its prompt
    version (D-014). Counsel: retrieve → verify (`read_clause` before quoting) →
    cite structurally → calculator for all math → typed abstention. Analyst:
    schema block embedded (with the native-currency/FX gotcha) so simple asks are
    one SQL round trip; royalty math explicitly out of scope. Reconciler:
    ingest → match → scan → allocations → submit, propose-only. Router: the
    four-way classify contract.
  - `configs.py` — per-agent tool sets (§4.3 matrix + D-013 additions), model
    policy from new settings (`PLANNER_MODEL`=sonnet-class, `UTILITY_MODEL`/
    `ROUTER_MODEL`=haiku-class), Reconciler workflow headroom (2x iterations/
    budget, 120s tool timeout, 4K-token results), finalizers: structural citation
    extraction (`FBR-C-00501 §3` patterns), first-line `ABSTAIN:` → typed
    abstention, Reconciler `BATCH:`/`FLAGS:` wrap-up → `ReconcilerAnswer
    (batch_id, flags_summary)` extending `FinalAnswer` (§4.2's Phase 4 shape).
  - `router.py` — the cheap-model front door: one forced `route` tool call →
    `{counsel|analyst|reconciler|clarify}` with honest confidence; below
    `ROUTER_CONFIDENCE_THRESHOLD` (0.6) it downgrades to clarify carrying the
    shadowed suggestion; malformed/missing tool calls degrade to clarify(0.0) —
    never a crash on model judgment. Traced as its own `router` run with metered
    `llm_call` span and the verdict in run meta.
  - `recall.py` + `dispatch.py` — §4.5 scope-3 auto-recall: router-detected
    artist names resolve exact-first; their `app.notes` fold into the user turn
    as a fenced `<recalled_notes>` block (trace shows exactly what the model
    saw); `route_and_run` = classify → recall → agent run (two traced runs per
    message by design), `clarify` short-circuits.
  - `injection.py` — §4.6 detection: regex families (role/override markers,
    instruction overrides, prompt/answer-key exfiltration, approval coercion)
    over document-bearing tool results only.
- Guardrails/runtime (additive): `ResultCheck` — post-execution, flag-don't-block
  policies; a hit records an `injection_suspected` guardrail span, marks the tool
  span, and prefixes the result with a one-line notice the model sees (D-013).
  Retrieval tools now fence quoted corpus text in `<document>` tags (search
  snippets and `read_clause` bodies).
- `backline/tools/scan.py` — `scan_anomalies`: the Reconciler's deterministic
  flag heuristics, one tolerance rule per §3.4 kind (D-013; dup-hash groups,
  catalog-miss ISRCs, feed-dialect currency reference from world.yaml, negative
  units, statement-period bleed, 4x-median fresh-territory spike threshold, 5%
  dashboard tolerance aggregated by statement period). Candidates carry evidence
  and per-source suggested exclusions; within-tolerance measurements are
  reported prose, explicitly *not* flags.
- `backline/tools/allocations.py` — `compute_allocations`: whole-period proposed
  allocations through `compute_ledger_slice` (one engine, D-001) with bounded
  concurrency (~7s for 149 artists), materiality floor (`min_net_payable`,
  default $0.01) with counted coverage of the zero/below-floor tail.
- Exclusions made per-source everywhere (label vs staged ids are separate,
  collidable sequences): `exclude_staged_line_ids` added to the ledger,
  `calc_royalties`, and `compute_allocations` (D-013).
- `scripts/ask.py` — manual poking harness: `--agent counsel "..."` direct or
  router-dispatched; prints route verdict, answer, citations, cost, run id;
  traces to Postgres + JSONL like production.
- Config/env: `PLANNER_MODEL`, `UTILITY_MODEL`, `ROUTER_MODEL`,
  `ROUTER_CONFIDENCE_THRESHOLD` (annotated in `.env.example`).
- Tests: 54 new — prompts/hashing, injection detector (+ whole-corpus
  false-positive sweep: exactly the FBR-C-00670 §7 canary trips across all
  2,961 chunks), runtime ResultCheck/trace-attrs, finalizers, router
  (confidence/threshold/fallbacks/trace), agent assembly (tool matrix,
  no-approval-path, limits), scan-vs-registry, allocations-vs-truth, canonical
  mock flows for all three agents, dispatch + recall, e2e all-tools run extended
  to the two new tools, and an 8-test live smoke suite (~10 questions, `-m
  live`) for the human run.

**Verified**

- `make lint` / `make typecheck` (mypy --strict, 135 files) green; full suite with
  `DATABASE_URL` against Postgres 16 + pgvector 0.8.6 (built from source in this
  sandbox): **389 passed, 10 deselected** (the 2 provider-live + 8 agent-live
  tests) — includes every Phase 1–3 suite; the world fingerprint is untouched.
- **Scan heuristics == answer key**: across all 12 seeded periods,
  `scan_anomalies` reproduces `truth.anomaly_registry` *exactly* — every
  registered non-borderline anomaly found under its kind (100% recall), zero
  unregistered flags (100% precision), and both borderline cases measured-but-
  not-flagged (the §3.4 precision trap, passed).
- **Allocations == answer key**: with registry-injected lines excluded,
  `compute_allocations` matches `truth.expected_ledger.net_payable` for every
  clean artist in the probe period, and lists every artist truth says to pay —
  D-001 proven through the batch path.
- Canonical flows (MockProvider, real tools, real Postgres): Counsel cites
  (`FBR-C-NNNNN §3` extracted into `FinalAnswer.citations`, prompt hash in run
  meta) and abstains typed on an unknown artist; the injection canary raises the
  `injection_suspected` guardrail span while the annotated clause text still
  reaches the model (flag-don't-block) and the scripted answer refuses it;
  Analyst answers a simple ask in exactly one `sql_query` round trip and
  recovers from a `truth.*` denial with a typed abstention; the Reconciler runs
  ingest → match → scan → compute → submit on a fresh `emit-period` month,
  lands a `proposed` batch (allocations + flags row-verified, run-stamped) with
  the statement still `received`, parses `BATCH:`/`FLAGS:` into
  `ReconcilerAnswer` — and stops: no tool in any agent's set can approve,
  reject, or promote (asserted).
- Router: confident routes pass through with the forced tool call pinned on the
  wire; low confidence downgrades to clarify with the suggestion preserved;
  malformed arguments and missing tool calls fall back to clarify; the classify
  run is traced and metered.
- e2e all-tools mock run now exercises all eleven tools in one traced run
  (adversarial `truth` query still dies as a `sql_policy` incident; every other
  tool executes cleanly; staging writes run-stamped).

**Live smoke (manual, 2026-08-06)** — the DoD's deferred live verification, run by
the maintainer on the dev machine against the real Anthropic API and the seeded
local Postgres (init cold-boot with the D-011 CPU-wheel image also verified
green). All 8 live agent tests passed:

```text
====================================================== test session starts ======================================================
platform linux -- Python 3.12.13, pytest-9.1.1, pluggy-1.6.0 -- /home/somx/code/backline/.venv/bin/python
cachedir: .pytest_cache
hypothesis profile 'default'
rootdir: /home/somx/code/backline
configfile: pyproject.toml
plugins: hypothesis-6.165.2, anyio-4.14.2, asyncio-1.4.0, cov-7.1.0
asyncio: mode=Mode.AUTO, debug=False, asyncio_default_fixture_loop_scope=None, asyncio_default_test_loop_scope=function
collected 40 items / 32 deselected / 8 selected

tests/agents/test_live_agents.py::test_live_counsel_terms_question_cites PASSED                                           [ 12%]
tests/agents/test_live_agents.py::test_live_counsel_abstains_on_fiction PASSED                                            [ 25%]
tests/agents/test_live_agents.py::test_live_counsel_math_goes_through_calculator PASSED                                   [ 37%]
tests/agents/test_live_agents.py::test_live_analyst_simple_ask_one_round_trip PASSED                                      [ 50%]
tests/agents/test_live_agents.py::test_live_analyst_never_reaches_truth PASSED                                            [ 62%]
tests/agents/test_live_agents.py::test_live_router_targets PASSED                                                         [ 75%]
tests/agents/test_live_agents.py::test_live_router_vague_message_clarifies PASSED                                         [ 87%]
tests/agents/test_live_agents.py::test_live_reconciler_scoped_ask_stops_at_proposal PASSED                                [100%]

========================================= 8 passed, 32 deselected in 118.36s (0:01:58) ==========================================
```

**Deviations / notes**

- **Live smoke not executed in the build session** — no API key in the sandbox by
  design (same protocol as Phase 2): `tests/agents/test_live_agents.py` ships 8
  structural checks (~10 questions: counsel cites/abstains/uses-calculator,
  analyst single round trip + truth distance, router targets + clarify,
  reconciler scoped propose-and-stop). The human runs
  `DATABASE_URL=... ANTHROPIC_API_KEY=... uv run pytest -m live tests/agents -v`
  once; results belong in this log — now recorded above (2026-08-06).
- `scan_anomalies` and `compute_allocations` extend §4.3's nine-tool matrix
  (Reconciler-only) — the plan's own "flag heuristics (tolerance rules per
  anomaly kind)" and period-scale allocation step need deterministic carriers;
  rationale + alternatives in D-013.
- Session-memory summarization (the §4.5 scope-1 utility-model hook) deliberately
  waits for Phase 6's sessions/API — there is no session construction site yet;
  the hook has existed since Phase 2 and the scope-3 tail (entity auto-recall)
  shipped here as planned (D-014).
- The sandbox's egress policy blocked the D-011 pre-task's lock/build steps (see
  pre-tasks above) — staged for the dev machine, not silently dropped.

**Deferred**: the live smoke paste (human, one-time); the D-011 re-lock
enable + image build (dev machine, procedure staged in pyproject + Dockerfile).

---

## Phase 5 — Eval Harness + Baselines + CI Gate (2026-08-06)

**Shipped**

- `evals/` — §5 in full, on the platform primitives (no new model paths; every
  LLM call is a traced, metered `Provider` run):
  - **Suite** (`types.py`, `generate_suite.py`, `worldfacts.py`): 133 questions
    across the ten §5.2 categories at exactly the plan's counts, generated
    offline from the in-memory world (the same objects the DB seeds from) plus
    25 hand-authored hard cases (`suites/hand_authored.json`) whose prompts are
    hand-written but whose expectations are *resolver-derived* from the answer
    key — no committed number can drift (D-015). The committed
    `evals/suites/core.json` is a golden artifact: content-hashed
    (`6eef41c6706f309a`), regenerated byte-for-byte in CI
    (`python -m evals generate --check`), with flagged subsets for the CI gate
    (43 questions, every hand case included) and the keyless smoke (10). Every
    prompt ends with an explicit output contract (`ANSWER:` / `FLAG:` lines);
    money questions only anchor on anomaly-untainted artists, and
    pay-over-threshold sets are boundary-checked against each tainted artist's
    worst-case corruption shift. `truth.qa_answer_key` (empty since Phase 1) now
    loads from the suite — `generate --load-db` and every runner start upsert it.
  - **Scorers**: T1 (`answers.py`, `scoring.py`) — mechanical extraction +
    tolerance money / exact counts / normalized percents / order-free sets /
    typed-abstention checks, and reconciliation as flag precision/recall/F1
    against the registry with the two borderline non-flags reported by name. T2
    (`trace_asserts.py`) — ten named span-tree checks (calculator-for-money,
    citations, clean SQL, no truth access, single/no batch, injection flagged +
    canary not obeyed, scan/allocations used); a `sql_policy` denial *is* the
    violation. T3 (`judge.py`, `judges/rubric_v1.md`) — pinned content-hashed
    rubric, forced `grade` tool call, judge model + rubric hash recorded per
    result; the judge sees cited clause texts fetched from the chunk store,
    never the expected answer. Question score = min(tier scores) (D-015).
  - **Runner** (`runner.py`): async under a concurrency semaphore, per-model,
    budget-guarded (§5.4: prints the projected spend from suite stats and
    refuses to exceed `--budget` without `--yes`; the budget is a hard stop
    mid-run), resumable (`--resume <eval_run_id>` skips scored questions),
    writing `app.eval_runs` (keyed suite_hash/model/git_sha) + one
    `app.eval_results` row per (question, tier) + streamed JSONL artifacts and a
    `summary.json` per run. Harness errors score the question zero and never
    kill the suite.
  - **Baselines** (`baselines.py`): B0 — no tools, a deterministic context
    packer greps the on-disk corpus (contract sidecars + CSV heads) and stuffs
    term-scored relevant-ish material to a token budget, recording how little of
    the 7.4M-token corpus fit; B1 — vector-only naive RAG (corpus-wide pgvector
    cosine, no governing filter, no FTS, no rerank, no calculator/SQL). Both
    reuse the typed-abstention/citation protocol and the T1 scorers; T2 recorded
    as not-applicable, T3 platform-only (D-015).
  - **Report + gate** (`report.py`, `gate.py`): per-category markdown tables and
    the multi-run comparison table (the §5.3 headline shape);
    `evals/results/baseline.json` committed with entries keyed
    (model, track, subset) pinning their suite hash; gate rules per §5.4 — fail
    on any category drop >3.0 pts, any T2 violation, stale suite hash, missing
    category, or budget-exhausted partial run; loud bootstrap-pass when no
    matching entry exists; `--write-baseline` upserts from a run summary
    (D-016).
  - **CLI** (`python -m evals`): `generate` / `run` / `smoke` / `gate` /
    `report`, wired like `scripts/ask.py` (real providers from settings, traces
    to Postgres + JSONL so eval runs are Trace-Inspector-inspectable).
- **`make eval-smoke`** (the §5.4 keyless plumbing test, now real): seeds
  `--if-empty`, ensures a chunk store, then runs the 10-question smoke subset
  through the *real* harness three times — platform (scripted MockProvider
  agents over real tools against seeded Postgres, judge scripted), B0, B1 — and
  gates each summary against the committed mock baseline. The committed
  `baseline.json` ships with those three all-100 entries, so the regression gate
  executes with teeth, keylessly, on every PR (D-016).
- **CI**: `test` job gains the suite-drift check + `make eval-smoke`;
  `eval-regression` is real (Postgres service, `--extra embed` CPU wheels, seed +
  real embeddings, `evals run --suite core --gate-subset --model claude-sonnet-5
  --budget 5.00 --yes --gate`, artifact upload) behind the step-level secret
  check; new `nightly-evals.yml` runs the full suite on main nightly
  (`workflow_dispatch` with a budget input, default $15).
- Config: `JUDGE_MODEL` setting (+ `.env.example`); Makefile `eval-smoke` real +
  new `eval-suite`; CLAUDE.md make-target list updated; TRACEABILITY rows for
  evaluation and CI/testing updated.
- Tests: 76 new in `tests/evals/` — suite generation (byte-exact reproduction,
  plan counts, tier/check shape, amendment coverage, DB-verified expectations:
  reference SQL, ledger fields, registry flag sets, paid-over sets, answer-key
  load), T1 scorers (tolerance boundaries, protocol violations, P/R/F1 math,
  borderline traps), T2 checks (each check both ways + unknown-name refusal),
  judge (grades parsed, malformed degradation, provenance pinning, no-expected-
  answer framing), runner (persist/aggregate, wrong-answer scoring, budget
  refusal, hard-stop + resume without duplicate rows, harness-error capture,
  B0/B1 tracks), packer units, gate units (the >3-pts boundary at exactly 3.0,
  T2 violations, stale hash, upserts), report rendering, and the smoke suite
  (scriptability guard, green-and-gated end-to-end, sabotage-trips-the-gate).

**Verified**

- `make lint` / `make typecheck` (mypy --strict, 157 files) green; full suite
  with `DATABASE_URL` against Postgres 16 + pgvector: **465 passed, 10
  deselected** (76 new; every Phase 1–4 suite included; world fingerprint
  untouched — the suite generator only *reads* the world).
- `python -m evals generate --check`: the committed 133-question suite
  reproduces exactly (hash `6eef41c6706f309a`); category counts match §5.2
  exactly (incl. 10 amendment-involving contract_terms questions); both seeded
  borderline anomalies appear as explicit non-flag expectations; the 38
  non-borderline registry entries are covered across the 12 period-scan
  questions.
- **The suite agrees with the database agents are evaluated on**: every analyst
  question's reference SQL reproduces its committed expectation against seeded
  Postgres; every ledger-derived money expectation equals `truth.expected_ledger`
  row-for-row; reconciliation flag sets equal `truth.anomaly_registry` grouped by
  statement period; multi_step paid-over sets equal the truth query.
- `make eval-smoke` end-to-end (sandbox, keyless): platform / B0 / B1 all score
  100.0 in every category through the real tools (platform metered $0.019440,
  p95 3.4s — real scan/allocation calls), and all three gates PASS against the
  committed baseline. The sabotage test proves the teeth: one mis-scripted
  answer drops royalty_math to 0, surfaces a T2 violation, and the committed
  gate fails on both counts while the untouched tracks stay green.
- The §5 DoD's injection requirement rides in the smoke: the adversarial
  question runs the real canary (FBR-C-00670 §7) through `read_clause`, the
  `injection_suspected` guardrail fires, and the T2 canary checks pass at 100.
- Budget guard behavior pinned by test: projection printed, refusal without
  `--yes` persists nothing, the hard stop mid-run marks the summary exhausted,
  and `--resume` completes the remainder with exactly one row per
  (question, tier).

**Deviations / notes**

- **The eval-regression job runs on the GitHub-hosted runner**, not the
  self-hosted runner BUILD_PLAN names — none is registered for this repo, and a
  job pinned to a non-existent runner queues forever (worse than a slower hosted
  run). Step-level secret gating is unchanged from Phase 0; the yaml carries the
  one-line `runs-on` switch for when a runner exists (D-016).
- **§5.4's "`--suite core`" reads as one suite with a gated subset**: the full
  133-question three-tier run projects past the $5 CI budget on the Sonnet tier,
  so the CI job runs the 43-question `--gate-subset` (every hand-authored hard
  case + per-category quotas, adversarial included) and the nightly workflow
  owns the full run — the plan's own budget number is the binding constraint.
- Reconciler multi_step questions anchor on 2025-07..2026-01: by the late window,
  tainted artists' *cumulative* corruption-shift bounds span every reasonable
  threshold, so no late period passes the boundary-safety check (the check is
  conservative by design; the early-window periods are provably safe).
- The smoke's Reconciler script ends with a placeholder `BATCH: 1` line — a
  scripted model cannot know the id the real `submit_batch` row received; T2
  asserts the actual span and staging write, and nothing scores the parsed id.
- Eval runs write real `staging` batches (multi_step questions submit through the
  one write path, run-stamped like any agent run) — they surface in the Review
  Queue as proposals, which is the honest behavior for a workflow eval.

**Deferred**: the first full live run (human, budget ≤ ~$15 per the DoD) — its
results table belongs in this log, and its `--write-baseline` output (live
`claude-sonnet-5` entries alongside the committed mock entries) should land in
the same commit; live judge (T3) behavior rides with it.

---

## Inter-phase (pre-Phase 6) — live eval run 2b9f39fb diagnosis + harness fixes · 2026-08-06

First full live run (`claude-sonnet-5`, suite `6eef41c6706f309a`, 133/133 scored,
$16.7358 metered vs $15 budget, overall 84.9) diagnosed. Three investigations;
harness fixes only — agent prompts and the committed suite untouched, so results
stay comparable under the same `suite_hash`.

**Shipped**

- **Pricing (bug 2a)**: `models.yaml` metered Sonnet 5 at the $3/$15 sticker while
  the API bills intro $2/$10 through 2026-08-31 — meter overstated spend exactly
  1.5× ($16.74 vs ~$11.16 billed). Registry now supports dated price schedules
  resolved per load date with the applied tier recorded and printed; tests pin
  both sides of the Sept 1 boundary (D-017). Side effect worth naming: the same
  1.5× inflated the per-iteration `RUN_BUDGET_USD` guardrail checks, squeezing
  the effective per-run cap to ~⅔ real-money intent — Reconciler multi_step runs
  ($1.00 cap ⇒ ~$0.67 effective) were the most exposed.
- **Budget hard stop (bug 2b)**: the gate read only landed cost, updated post-
  completion; with concurrency 4 and p50 15s / p95 115s, in-flight multi_step
  costs were invisible and all 133 questions started. Gate now reads committed
  spend (landed + per-question projected reservations), announces the stop, and
  warns on overshoot; regression test proves the stop trips at concurrency 4
  with costs in flight, and was verified failing pre-fix (D-019).
- **Projection (bug 2c)**: $6.86 reproduced exactly from the old `_PROJECTION`
  constants — single-round-trip token guesses that ignore per-iteration context
  resend. Recalibrated to loop totals (~2.4×, provisional pending per-agent
  means from the run artifacts); projection refactored to a per-question unit
  shared with the reservations (D-019).
- **Abstention protocol (investigation 1)**: code-verified conflict — abstention
  prompts demand a closing `ANSWER:` line (the D-015 trap) while the finalizer
  accepted `ABSTAIN:` on the first line only; Phase 4's live smoke prompt had no
  format suffix, which is why it passed. Finalizer now accepts the typed
  abstention opening *or* closing the reply, mid-text still refused (D-018).

**Trace adjudication (operator-pasted `results.jsonl` excerpts, same session)**

- **Abstention: 9/9 failures were harness artifacts; zero hallucinations.**
  Every failing reply carried an explicit, correct typed `ABSTAIN:` line —
  seven displaced to just above a placeholder final `ANSWER:` line
  (`N/A%`, `$0`, `NO`, `0`, `N/A`), two jammed into the answer line itself
  (`ANSWER: ABSTAIN: …`); the one pass led with it. The finalizer now accepts
  all observed shapes (mid-reasoning mentions still refused), pinned by a test
  carrying the verbatim trace patterns. Agent behavior needs no change.
- **multi_step T1 split: 6/6 failures are `run_exhausted`, zero extraction
  failures, zero wrong sets.** All six reconciler workflow questions died at
  the per-run budget guardrail ($1.03–1.10 metered vs the $1.00 cap ⇒
  ~$0.69–0.73 actually billed vs the cap's $1.00 intent — the pricing bug
  shrank effective headroom 1.5×), each after completing scan + allocations
  but before submit (T2 `single_batch: 0`), leaving an empty answer for T1 and
  the judge. All six counsel spot-quotes passed T1+T2 with delta 0.000000; the
  category's remaining deductions are judge marks on two counsel answers'
  overreach/hedging (multi_step-009 at 0.53 failing, -007 at 0.60 passing) —
  genuine prose-quality findings, not harness bugs. One stray T2 note:
  multi_step-003 probed `information_schema` (correctly denied → `sql_clean`
  fail).
- **Enforcement, empirically closed**: the seven long reconciler runs ($7.12
  combined) landed last; cumulative landed spend first exceeded $15 at result
  132/133 — the landed-only gate could never have fired. Reservations fix this
  class.
- **`_PROJECTION` finalized** from judge-subtracted per-agent means (counsel
  14k/1.8k, analyst 4.5k/750, reconciler 87k/12.7k): suite total $16.90 vs
  $16.74 metered; at intro prices $11.27 vs ~$11.16 billed. Reconciler mean is
  cap-censored (6/22 runs capped) and floors reconciler-heavy projections.
- reconciliation shows no exhaustion signature (max $0.344 ≪ cap) — its single
  T1 miss is a genuine flag-set miss; no harness action.
- No baseline written; no live re-run executed from this session (per operator
  instruction). Re-run recommendation recorded in the session report:
  `--categories abstention,multi_step`.

## Inter-phase (pre-Phase 6) — eval run ddb797dc Reconciler diagnosis + truncation/path fixes · 2026-08-06

Diagnosed the multi_step reconciler failure that survived D-020's budget raise
(run ddb797dc: 5/6 `run_exhausted` at $1.20–1.40 — under the $2.50 cap, so the
iteration cap was the binder — and multi_step-003 `no_answer_line` at $0.22).
Root cause measured against the seeded world, not guessed from traces: every
full period pays 95–103 artists, so the verbatim `submit_batch` allocation
payload (~13k chars ≈ 3.2k tokens at the len/4 floor, before flags/note/
preamble) cannot stream inside the 4096-token output ceiling — and the runtime
ignored `stop_reason=max_tokens`, so cut tool calls partial-parsed into
`invalid_tool_args` retry loops (exhaustion) and one cut text reply finalized
as a "completed" answer with no `ANSWER:` line (multi_step-003). Full write-up
in D-021.

**Shipped**

- **Runtime truncation contract (D-021)**: a `max_tokens`-cut reply is never
  acted on — tool calls discarded un-executed (keyed off `stop_reason`; a
  streamed-prefix dict can validate and still be wrong, which is also how a
  silently partial batch could have submitted), truncated text never finalized;
  both paths return explicit notices, record `output_truncated` guardrail
  spans, and consume their iteration. Three runtime tests, including the
  valid-prefix discard case.
- **Reconciler `max_tokens` = 16384** (others stay 4096): fits the measured
  worst-case submit with ~3× margin; pinned in the config test.
- **Path anchoring (D-022)**: relative artifact/trace/data paths anchor at the
  repo root (`repo_root`/`anchor_path`/`Settings.data_path`) — fixes eval
  artifacts nesting inside an old run dir when launched from there
  (`data/evals/127c5ad8…/data/evals/ddb797dc…`); applied to eval artifacts,
  trace JSONL sink, B0 corpus index, ingest inbox, embed pipeline; absolute
  configs (compose `/data`, test `tmp_path`) untouched. Chdir regression test
  mirrors the observed nesting.

**Deferred / unchanged**

- Suite, prompts, tool contracts, budgets, iteration caps: untouched —
  `suite_hash` comparability holds. Allocations-by-reference for
  `submit_batch` noted in D-021 as a future design change, not a bug fix.
- No live run, no baseline write (operator instruction); the multi_step
  re-run and `_PROJECTION` recalibration stay on the operator's list.
- **Span-tree confirmation (operator pull, post-diagnosis)**: the ddb797dc
  spans match the pre-registered signature exactly — 68 `max_tokens` stops at
  4096 output tokens across the six questions; the five exhausted runs each
  show 14–16 `submit_batch` denials, every one `invalid_tool_args:
  allocations Field required` (the cut always severed the whole `allocations`
  field; the parsed prefix held only `period`), ending at `iteration 25
  exceeds max_iterations=24`; multi_step-003 ran cleanly through
  `compute_allocations` and ended on a single mid-text `max_tokens` cut with
  no tool_call after it. Diagnosis confirmed; investigation closed.

---

## Inter-phase (pre-Phase 6) — eval detour close-out: multi_step green (run c804b338), composite baseline protocol (D-023) · 2026-08-06

Closes the diagnosis arc opened by run 2b9f39fb. Four runs, four harness bugs,
zero agent hallucinations found anywhere in the adjudicated traces.

**Final re-run (c804b338, `--categories multi_step`, git `6567d9ad9cba`) — the
fixes hold.** All six Reconciler workflows ran ingest → match → scan →
allocations → `submit_batch` to completion, submits landing normally — no
`run_exhausted`, no truncation retry loops. Operator-pasted result:

- platform/claude-sonnet-5 (full) · suite `6eef41c6706f309a` · run
  `c804b338-a874-42fe-853e-f855281be5e0`
- 12/12 questions scored · spend $2.755974 (budget $14) · latency p50 110729ms
  / p95 127570ms
- T2 violations: 1 · judge: claude-sonnet-5 (rubric ffe8c9753172)

| category | n | score | T1 | T2 | T3 |
|---|---:|---:|---:|---:|---:|
| multi_step | 12 | 72.8 | 100.0 | 98.3 | 72.8 |
| **overall** | 12 | **72.8** |  |  |  |

artifacts → `data/evals/c804b338-a874-42fe-853e-f855281be5e0` (operator host)

**The arc, in one place** (full write-ups in the two entries above and
D-017..D-022):

- **2b9f39fb** — first full live run: 133/133, overall 84.9, $16.74 metered vs
  the $15 budget. Bug 1: **dated pricing** (D-017 — the meter billed sticker
  $3/$15 while the API charged intro $2/$10; every metered number 1.5× real).
  Bug 2: **budget gate blind to in-flight spend** (D-019 — landed-cost-only
  reads meant the hard stop could never fire; it now reads committed spend =
  landed + per-question reservations). Alongside: D-018, after adjudication
  showed all 9 abstention failures were harness artifacts — every failing
  reply carried a correct typed `ABSTAIN:` line the finalizer refused on
  placement.
- **127c5ad8** — targeted re-run (abstention + multi_step, correct prices):
  abstention 100.0, so D-018 holds; all six reconciler questions exhausted at
  the now-correct $1.00 cap. Bug 3: **per-run budgets were guesses** (D-020 —
  budgets are sized empirically; Reconciler floor $2.50).
- **ddb797dc** — multi_step re-run: 5/6 exhausted *under* the raised cap on
  iteration burn. Bug 4: **truncation contract + output sizing** (D-021 — the
  runtime acted on `max_tokens`-truncated replies, so cut `submit_batch`
  calls partial-parsed into `invalid_tool_args` retry loops, and a
  full-period allocations payload cannot fit a 4096-token ceiling; truncated
  replies are now never acted on and the Reconciler ceiling is 16384).
  D-022 path anchoring was found and fixed en route; the span-tree pull
  matched the pre-registered signature exactly.
- **c804b338** — green (table above).

**What the arc did not find: agent failures.** Every diagnosed zero traced to
the harness (meter, budget gate, caps, truncation handling); adjudicated agent
behavior was correct throughout, including all abstentions. What remains in
multi_step is T3 prose quality — 72.8 from judge marks of the
overreach/hedging kind first seen on the 2b9f39fb counsel answers — now the
category's floor with T1/T2 at 100.0/98.3. That is future prompt-tuning, not a
defect; noted, not actioned (agent prompts stayed untouched all arc, so
`suite_hash` comparability holds). Standing sharp edge, also untouched: the
gate fails any run with T2 violations > 0 by design, and c804b338 carries one
(98.3) — a future *gated* run needs a violation-free pass, while the baseline
below records scores as measured.

**Baseline: composite protocol shipped (D-023).** The gate's staleness
identity is the `suite_hash`, and all four runs answered committed suite
`6eef41c6706f309a` — so the latest valid measurement of every category already
exists: eight categories from 2b9f39fb (adjudicated clean; harness-only fixes
since), abstention from 127c5ad8, multi_step from c804b338. Rather than
re-buying those numbers with a ~$11 fresh full run, `python -m evals compose`
(new, test-first, 13 tests including composed-entry-feeds-the-gate) merges run
summaries into one gate-ready summary under hard refusal checks — one
(model, track, subset, suite_hash) shape, committed-suite hash match, full
per-category counts, exact coverage, later-runs-override-per-whole-category,
per-category provenance folded into the entry note.

**Left open — one keyless $0 command on the operator host** (this session ran
in a remote container: no run artifacts, no API key; live spend authorized up
to $15 was not needed):

    python -m evals compose \
      --summary data/evals/2b9f39fb-*/summary.json \
                data/evals/127c5ad8-*/summary.json \
                data/evals/c804b338-a874-42fe-853e-f855281be5e0/summary.json \
      --write-baseline --note "first live baseline (post-diagnosis composite)"

then commit `evals/results/baseline.json`. Order is precedence (oldest first);
ddb797dc is deliberately excluded (its multi_step was invalidated by D-021).
If an older run's `summary.json` moved (pre-D-022 CWD nesting), recover it via
`SELECT summary FROM app.eval_runs WHERE id = '<run id>'`. Separately and
unchanged by this close-out: the `(claude-sonnet-5, platform, gate)` CI entry
still bootstrap-passes until a ~$5 `--gate-subset` run is recorded.
