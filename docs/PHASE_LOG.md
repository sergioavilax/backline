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
