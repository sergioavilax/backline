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
