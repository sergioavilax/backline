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
