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
