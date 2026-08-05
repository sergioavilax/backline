# CLAUDE.md — how to work in this repo

Backline is built phase-by-phase from [BUILD_PLAN.md](BUILD_PLAN.md). Read the plan's §0
before doing anything; this file is the operational summary that governs every session.

## Session protocol

- **One phase per fresh session.** Prompt shape: *"Read BUILD_PLAN.md and CLAUDE.md.
  Execute Phase N exactly. Do not start Phase N+1."*
- Each phase ends with: full test suite green, `docs/PHASE_LOG.md` appended (what shipped,
  what was deferred, any deviation + why), `docs/DECISIONS.md` appended for any judgment
  call, and one PR.
- If a phase is too large for one session: finish a coherent sub-slice, mark the remainder
  explicitly in PHASE_LOG, stop cleanly. **Never leave the suite red.**

## Non-negotiable invariants (BUILD_PLAN §0 — apply to every change)

1. **Money is never float.** `Decimal` in Python, `NUMERIC(18,6)` in Postgres. Line-level
   amounts keep 6 decimal places; artist-facing totals round half-even to cents at final
   aggregation only. One rounding policy, once, in `backline/royaltycalc/rounding.py`.
2. **One implementation of royalty math.** `royaltycalc` is the single source of truth;
   both datagen's truth engine and the runtime calculator tool import it (D-001).
3. **Agents can never read the answer key.** Ground truth lives in the `truth` schema;
   the SQL tool's allowlist excludes `truth.*` at the parser level, with a test asserting
   it stays excluded. An agent touching `truth` = eval failure + guardrail incident.
4. **Deterministic world.** Everything derives from `WORLD_SEED` (default `20260805`).
   No `random`/`np.random` module calls outside the seeded `numpy` Generator threaded
   through datagen.
5. **All writes are gated.** Agents propose; humans approve. Mutations go to `staging`
   tables and the Review Queue. Nothing promotes without an explicit approval action.
6. **Everything is traced.** Every run emits span events (run → iteration → LLM/tool call)
   with tokens, cost, latency. **No silent LLM calls anywhere** — and no LLM call outside
   a `Provider` implementation in `backline/providers/`.
7. **Phases are additive.** Later phases never strip or simplify shipped functionality.
   Refactors allowed only with tests green and behavior preserved.
8. **Tests gate every PR.** Unit/integration tests never require an API key (use
   `MockProvider`). Live-model evals run behind the `ANTHROPIC_API_KEY` secret only.

## Engineering conventions

- **Test-first for core modules** (`royaltycalc`, `core/`, `tools/`, `rag/`, `evals/`):
  write the failing test, then the implementation.
- **Conventional commits**: `feat(scope): ...`, `fix(scope): ...`, `test:`, `docs:`,
  `chore:`, `ci:`. One phase = one PR.
- **Python**: 3.12, `uv` for env/locking, `ruff` (lint + format), `mypy --strict`,
  `pytest` + `pytest-asyncio`. Run `make lint typecheck test` before committing.
- **UI**: Next.js 15 + TypeScript + Tailwind, `pnpm`. UI work follows
  `docs/UI_DIRECTION.md` (created in Phase 6; aesthetic brief in BUILD_PLAN §6).
- **Migrations**: raw SQL files in `migrations/`, applied in filename order by
  `python -m backline.db.migrate` (see D-000). Never edit an applied migration; add a new one.
- **Line endings**: LF everywhere (`.gitattributes` enforces; repo developed under WSL2).

## Where things live

- Decisions (ADR-style, numbered): `docs/DECISIONS.md` — append a D-NNN entry for every
  judgment call that isn't already specified by BUILD_PLAN.
- Phase log: `docs/PHASE_LOG.md` — append at the end of every phase.
- Job-spec traceability: `docs/TRACEABILITY.md`.
- Make targets: `make help` (`up`, `test`, `lint`, `typecheck`, `doctor`; `seed`/`embed`/
  `eval-smoke`/`corpus-tokens` are phase-gated stubs until their phase ships).

## Commands

```bash
make doctor      # environment sanity (docker, ports, env, line endings)
make up          # compose stack: db + init(migrations) + api + ui
uv sync          # local Python env
make test        # pytest — Postgres tests skip unless DATABASE_URL is set
make lint        # ruff check + format --check
make typecheck   # mypy --strict
cd ui && pnpm dev
```
