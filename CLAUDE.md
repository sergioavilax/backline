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
- Make targets: `make help` (`up`, `test`, `lint`, `typecheck`, `doctor`, `seed`,
  `emit-period`, `corpus-tokens`, `embed`, `retrieval-probe`, `eval-smoke`,
  `eval-suite`). The eval harness CLI is `python -m evals` (generate / run / smoke /
  gate / compose / report).

## Commands

```bash
make doctor      # environment sanity (docker, ports, env, line endings)
make up          # compose stack: db + init(migrations+seed) + api + ui
uv sync          # local Python env
make seed        # build the deterministic world into Postgres + ./data (< 3 min)
make test        # pytest — Postgres tests skip unless DATABASE_URL is set
make lint        # ruff check + format --check
make typecheck   # mypy --strict
cd ui && pnpm dev
```

World determinism: `tests/golden/world_fingerprint.json` pins the seeded world's content
hash. If a PR intentionally changes generation, regenerate it via
`python -m datagen fingerprint --files > tests/golden/world_fingerprint.json` and say so
in the PR; an unexplained golden diff means the answer key silently moved.

---

## Appendix — AWS deploy (`AWS_DEPLOY_PLAN.md`)

The AWS epilogue is governed by [AWS_DEPLOY_PLAN.md](AWS_DEPLOY_PLAN.md), a companion
to BUILD_PLAN with the same discipline: one session per phase (A0–A6), one PR per
PR-marked phase, a `PHASE_LOG.md` entry per phase. **The deployment was executed and
destroyed on 2026-08-08** — the artifact is the repo, not a running service. Its
verdict lives in [deploy/aws/README.md](deploy/aws/README.md).

**Invariants (AWS_DEPLOY_PLAN §0.2) — these bind every AWS session:**

1. **Never run `terraform apply` or `terraform destroy`.** Claude Code writes `.tf`
   files and may run `fmt`, `init`, `validate`, and `plan`. The human applies and
   destroys, and reads every plan end to end first. This is not a formality: it is
   the only control preventing an agent from creating billable or destructive cloud
   state.
2. **The Anthropic key never enters the repo, the Terraform state, or a task
   definition's plain `environment` block.** It lives in Secrets Manager, is set by
   the human out of band, and is injected via the ECS `secrets` (`valueFrom`)
   mechanism. Terraform creates an empty secret shell and never learns the value.
3. **Nothing existing is modified unless the plan explicitly says so.** The deploy is
   additive — new files under `deploy/aws/`, one Dockerfile under `docker/`, docs.
   Local dev keeps working identically.
4. **Ingress is locked to the operator's home `/32`.** No `0.0.0.0/0` ingress on any
   security group, ever. That lock is what makes a live key behind a public URL safe.
5. **Parity is measured, not asserted.** Claims about AWS-vs-local behaviour cite the
   repo's own documented noise floor (`docs/BENCHMARK_NOTES.md` §5.4). Both runs
   publish whatever they say; a gate failure is reported and adjudicated, never
   re-rolled until it passes.

**Where the deploy files live:**

- `deploy/aws/*.tf` — the Terraform root module, one file per concern.
- `deploy/aws/scripts/` — `build_push.sh`, `run_eval_task.sh`, `fetch_summary.sh`.
- `deploy/aws/evidence/` — the AWS run summary and the day's screenshots.
- `deploy/aws/README.md` — the parity table, decisions, and the what-broke log.
- `docker/aws.Dockerfile` (+ its per-Dockerfile `.dockerignore`) — the deploy image:
  the API image plus `evals/`, baked HF model weights, and the baked deterministic
  `/data`. Never edit `docker/api.Dockerfile` to serve a deploy need.
- **Gitignored, never committed:** `deploy/aws/terraform.tfvars` (carries the
  operator's home IP), `*.tfstate*`, `.terraform/`, `backline.dump`.
  `.terraform.lock.hcl` *is* committed.
