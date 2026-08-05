# Backline

**An agent platform for music label operations** — orchestration, tool-use, memory, routing,
guardrails, RAG, and eval-gated CI over the three datasets every label lives on: contracts,
catalog, and royalty statements — with a human-in-the-loop workflow for the money-moving parts.

> In music, the *backline* is the gear behind the band — the amps, the drums, the infrastructure
> that makes the show possible without ever being the show.

**Status: Phase 0 (repo skeleton).** The full build plan, architecture, and phase-by-phase roadmap
live in [BUILD_PLAN.md](BUILD_PLAN.md). The real README ships in Phase 8, with results tables.

## Quickstart

```bash
git clone https://github.com/sergioavilax/backline && cd backline
cp .env.example .env        # optional — everything has working defaults
make doctor                 # verify docker, ports, line endings
make up                     # db + migrations + api + ui
```

Then: API health at <http://localhost:8000/healthz>, UI at <http://localhost:3000>.

## Development

```bash
uv sync            # Python 3.12 env (uv)
make test          # pytest (Postgres tests skip unless DATABASE_URL is set)
make lint          # ruff check + format
make typecheck     # mypy --strict
cd ui && pnpm dev  # UI dev server
```

Conventions and invariants for contributors (human or agent): see [CLAUDE.md](CLAUDE.md).
Decisions log: [docs/DECISIONS.md](docs/DECISIONS.md) · Phase log: [docs/PHASE_LOG.md](docs/PHASE_LOG.md) ·
Job-spec traceability: [docs/TRACEABILITY.md](docs/TRACEABILITY.md)
