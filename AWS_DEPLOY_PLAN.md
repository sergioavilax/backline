# BACKLINE ON AWS — AWS_DEPLOY_PLAN.md

**One-day objective:** deploy the existing Backline stack (API + UI + Postgres/pgvector) onto AWS with Terraform, restore the exact local world into RDS, run the full 133-question eval suite *on AWS infrastructure* as a one-off ECS task, prove score parity against a same-day local control run, capture evidence, and tear everything down. The artifact is the `deploy/aws/` Terraform tree, the parity table, and the "what broke" log — not a running service.

> Companion to `BUILD_PLAN.md` (the original 9-phase build). Same process: one Claude Code session per phase, one PR per PR-marked phase, a human verification gate closes every phase. This file governs every AWS deploy session the way BUILD_PLAN.md §0 governs build sessions.

---

## 0. Read Me First (governs every session)

### 0.1 What this is / is not

**Is:** a reviewable Terraform root module, a deploy-specific Docker image, a runbook that was actually executed, an eval-verified migration, and an honest writeup. Everything a hiring manager reads lives in the repo after `terraform destroy`.

**Is not:** a hosted product, a public demo, an always-on service, Kubernetes, or Lambda. No autoscaling, no CI/CD-to-AWS, no custom domain, no HTTPS/ACM. Those are deliberate scope cuts, each recorded in the writeup as "what production would add."

### 0.2 Non-negotiable invariants

1. **The human runs every `terraform apply` and `terraform destroy`.** Claude Code writes `.tf` files, runs `terraform fmt` and `terraform validate`, and may run `terraform plan`. It never applies, never destroys, and never receives AWS credentials with write scope beyond what `plan` needs. If Claude Code is run without AWS credentials at all (recommended), the human also runs `plan` and pastes output back.
2. **The Anthropic key never enters the repo, the Terraform state, or a task definition's plain `environment` block.** It lives in AWS Secrets Manager, set by the human via CLI, injected via the ECS `secrets` (valueFrom) mechanism.
3. **Nothing existing is modified unless this plan explicitly says so.** `docker-compose.yml`, `docker/api.Dockerfile`, `ui/Dockerfile`, and all runtime Python are untouched. The deploy is additive: new files under `deploy/aws/`, one new Dockerfile under `docker/`, docs. Local dev keeps working identically. (Exception: the one stale comment fix in §A1.4, which changes zero behavior.)
4. **Ingress is locked to the operator's home IP for the entire exercise.** The ALB security group admits ports 80 and 8000 from `var.home_cidr` (a /32) only. This is what makes a live Anthropic key behind a public URL safe: the URL is public in name only.
5. **Money rules carry over.** The eval budget is money → the run command uses the committed sweep sizing (`--budget 20.00`, sized for standard-tier pricing per `benchmarks/sweep.yaml`). An AWS Budget with a $25 threshold exists before the first `apply`.
6. **Parity is measured, not asserted.** The claim is defined in §A5.5 using the repo's own documented noise floor (BENCHMARK_NOTES §5.4: same-model/same-suite spread of 3.2 overall points; pre-registered "≤ ~3 points is noise"). No cherry-picking: both runs publish, whatever they say.

### 0.3 Division of labor

| Claude Code writes | Human (Sergio) does |
|---|---|
| `docker/aws.Dockerfile` + its dockerignore | Creates AWS account artifacts (A0 checklist) |
| Everything under `deploy/aws/*.tf` | Runs `terraform apply` / `destroy`, reads every plan |
| Runbook scripts under `deploy/aws/scripts/` | Sets the Anthropic key in Secrets Manager |
| Verification one-liners, expected outputs | Runs the dump/restore against RDS |
| `deploy/aws/README.md`, root README section, CLAUDE.md amendment | Pushes images, launches the eval task, captures screenshots |
| The parity table + writeup drafts from real outputs | Pastes AWS errors back into the session (the human is the feedback loop) |

### 0.4 Verified facts this plan is built on

Everything below was verified on 2026-08-08 by executing the repo in a cold Linux sandbox (fresh Postgres 16.14 + pgvector built from source) and against AWS documentation. Claude Code sessions must treat these as ground truth and must not "improve" around them.

| # | Fact | Consequence in this plan |
|---|---|---|
| V1 | `docker/api.Dockerfile` copies only `backline datagen migrations config`. `evals/` is **not** in the image; `python -m evals` cannot run there, and the API's `/evals/baseline` route reads `evals/results/baseline.json` from disk via `repo_root()` and would 500/404 on AWS. | `docker/aws.Dockerfile` adds `COPY evals ./evals`. One line fixes both the eval task and the deployed dashboard. |
| V2 | The image installs `--extra embed` with CPU torch (lockfile resolves torch from `download.pytorch.org/whl/cpu` — the pyproject comment claiming the index block is "commented out" is stale), but **no model weights are baked**; bge-small + the ms-marco cross-encoder download from Hugging Face on first use. | `docker/aws.Dockerfile` pre-downloads both models into `HF_HOME=/opt/hf` at build time; the A1 gate proves the container loads them with `HF_HUB_OFFLINE=1`. |
| V3 | `datagen seed --if-empty` checks **only the DB** (`label.artists` non-empty). With RDS restored from a dump it skips everything, including rendering `/data/contracts` and `/data/inbox`. The Reconciler's ingest tool reads `settings.data_path/"inbox"/<file>` at runtime; without inbox files, every reconciliation eval question fails. `data/` is excluded by the root `.dockerignore`. | `/data` (32 MB, deterministic, fingerprinted) is **baked into the deploy image** via a per-Dockerfile ignore file (`docker/aws.Dockerfile.dockerignore`, BuildKit). Traces/evals dirs pre-created. |
| V4 | RDS for PostgreSQL 15+ defaults `rds.force_ssl = 1`. asyncpg accepts `?sslmode=require` in the DSN (verified: parses and negotiates). `psql`/`pg_restore` default `sslmode=prefer` and auto-upgrade, so the restore path needs nothing special. | Every AWS-side `DATABASE_URL` is built with `?sslmode=require` (in the Secrets Manager secret). We comply with force_ssl rather than disabling it — better story, zero param-group work. |
| V5 | `NEXT_PUBLIC_API_URL` is baked into the Next.js bundle at build time; Fargate task public IPs change on every restart. | One ALB, **two listeners** (`:80 → UI`, `:8000 → API`) gives both services a stable DNS name with no domain/cert needed. UI image is built *after* `apply` outputs the ALB DNS. |
| V6 | The eval runner (`python -m evals run`) talks directly to Postgres + Anthropic — it does not go through the HTTP API. Results persist to `app.eval_runs` / `app.eval_results`; `eval_runs.summary` (jsonb) is updated at completion; the deployed UI's Eval Dashboard reads those rows. | "Run the suite on AWS" = a one-off `aws ecs run-task` with a command override on the same image. Artifacts are recovered from RDS with one `psql` query (Fargate storage is ephemeral); the run is *also* visible in the deployed dashboard for screenshots. |
| V7 | Measured on cold hardware: migrations ~1 s; full seed 28 s (571,776 rows, fingerprint `f7a0b877…`); embed builds 2,961 chunks from 385 contracts; **`pg_dump -Fc` = 17 MB**, dump 2 s, restore 5 s, all counts survive (`statement_lines` 468,160 · `contract_chunks` 2,961 · `truth.expected_ledger` 1,800); DB 130 MB; `/data` 32 MB (contracts 4.3 MB, inbox 28 MB = 72 drops); keyless `evals smoke` green end-to-end on the fresh box. | The migration is trivial in size. Restore-from-home over the internet is minutes. The verification queries in A3 use these exact expected numbers. |
| V8 | `schema_migrations` is included in the dump → `migrate` against restored RDS is a no-op. The ivfflat index and `ANALYZE` were done by the embed job locally and travel inside the dump. RDS PG16 (16.5+) ships pgvector 0.8.x incl. ivfflat; the dump's `CREATE EXTENSION` is version-unpinned. | No migrate step, no re-embed, no re-index on AWS. Pre-create the extension, restore, verify. |
| V9 | Query-time search reads the stored `embedding_model` from `rag.contract_chunks` and must embed queries with the same model. The deploy env pins `EMBED_MODEL=BAAI/bge-small-en-v1.5`, `RERANK_MODEL=cross-encoder/ms-marco-MiniLM-L-6-v2`, `RERANK=on` (the defaults). | A0 pre-flight asserts the **local** DB's stored model is the bge model *before* dumping (if a stale `hash-bow-384-v1` store is found, run `make embed` first). |
| V10 | Gate semantics: per-category drop > 3.0 pts vs baseline → fail; any `t2_violations > 0` → fail; any quarantined infra error → fail until healed via `--resume <id> --retry-errors` (D-032); suite-hash mismatch → fail. Live sweep rows recorded 3–7 T2 flickers, and the documented same-model spread is 3.2 overall points — so a *legitimate* fresh run can fail the strict gate on variance alone. Full-run cost receipts: Sonnet $8.09 total at intro pricing ($2/$10 through 2026-08-31), projection $11.27 < the $20 budget → no `--yes` needed. | §A5.5 defines parity as the **paired-run comparison** (fresh local control vs fresh AWS treatment, same day) judged against the noise floor; the gate is run and reported as the strict secondary check with its known variance failure modes pre-declared. Both runs use identical flags: `--suite core --model claude-sonnet-5 --budget 20.00`, default concurrency 4, default judge. |
| V11 | `git` is absent in the image; the runner falls back to the `GITHUB_SHA` env var for run attribution, else records NULL. | Task definitions set `GITHUB_SHA` from `var.deployed_git_sha` so AWS eval rows are attributable to the deployed commit. |
| V12 | `JsonlSink` mkdirs its trace directory; `repo_root()` in the image resolves to `/app` (pyproject.toml is copied), so `/app/evals/results/baseline.json` is found once V1's COPY lands; models load lazily on first search (first query after deploy pays a one-time CPU model load — expected, not a bug). | No code changes needed for paths. Note the first-query latency in the runbook so nobody debugs a non-problem. |
| V13 | Terraform destroy gotchas: `aws_ecr_repository` refuses to delete with images unless `force_delete = true`; `aws_s3_bucket` refuses with objects unless `force_destroy = true`; Secrets Manager secrets linger (and bill) in a recovery window unless `recovery_window_in_days = 0`. | All three flags are set in the Terraform from the start so teardown is one clean `destroy`. |
| V14 | Pricing (verified 2026-08): Fargate $0.04048/vCPU-hr + $0.004445/GB-hr; ALB ≈ $0.0225/hr + LCU; RDS `db.t4g.micro` ≈ $0.016/hr. CloudWatch **billing alarms** only work from us-east-1 — use **AWS Budgets** (global) instead. | Cost table in §8; A0 uses AWS Budgets, not a billing alarm. |

### 0.5 Region, naming, PR map

- **Region:** `us-west-2` (Oregon). Closest sane region to LA; us-west-1 is pricier with fewer AZs. Every command and the provider block pin it.
- **Name prefix:** every resource is `backline-*` and carries `Project = "backline"` and `Ephemeral = "true"` tags (tag block is `default_tags` on the provider — one place).
- **PRs:** PR-1 = Phase A1 (deploy image). PR-2 = Phase A2 (Terraform tree + scripts + runbook skeleton). PR-3 = Phases A5–A6 (evidence, parity table, writeup, CLAUDE.md amendment). Phases A3/A4 are runbook execution — their outputs land in PR-3.
- **Session protocol:** one fresh Claude Code session per phase. Each session starts by reading this file and `CLAUDE.md`, ends by updating `docs/PHASE_LOG.md` with an `A<n>` entry (same log discipline as the build phases).

---

## Phase A0 — Account prep (human, night before, ~45 min)

No Claude Code session. Checklist; every box must be checked before A1 starts.

- [ ] **AWS account** exists; root MFA on; an IAM user or (better) IAM Identity Center user `sergio-admin` with `AdministratorAccess` for the day. Access keys configured locally: `aws configure` → region `us-west-2`, output `json`. Verify: `aws sts get-caller-identity` returns your account.
- [ ] **AWS Budget:** Console → Billing → Budgets → monthly cost budget, amount **$25**, email alert at 80% and 100%. (Not a CloudWatch billing alarm — those only live in us-east-1; Budgets are global. V14.)
- [ ] **Terraform** ≥ 1.9 installed in WSL2 (`terraform version`).
- [ ] **Docker + BuildKit:** `docker buildx version` works (any 2024+ Docker Desktop/Engine does). Needed for the per-Dockerfile ignore file (V3).
- [ ] **Home IP captured:** `curl -s ifconfig.me` → write it down; this becomes `home_cidr = "<ip>/32"` in tfvars. (If your ISP rotates it mid-day: edit tfvars, `terraform apply` — it's a 20-second security-group update. In the failure playbook.)
- [ ] **Local stack healthy:** `make up` green, `curl -s localhost:8000/readyz` → `{"status":"ok","database":"ok"}`.
- [ ] **Pre-flight: the local DB carries real embeddings** (V9):
  ```bash
  docker compose exec db psql -U backline -d backline -t -c \
    "SELECT DISTINCT embedding_model FROM rag.contract_chunks;"
  ```
  Expected: `BAAI/bge-small-en-v1.5`. If it says `hash-bow-384-v1`, run `make embed` (host side, with the `embed` extra synced) and re-check. **Do not dump a hash-embedded store.**
- [ ] **Pre-flight: baseline & suite intact:** `uv run python -m evals generate --check` exits 0.
- [ ] **Anthropic key** at hand (the same one the local runs used), with enough headroom for ~$20 of eval spend on top of normal use.
- [ ] Note the deploy SHA: `git rev-parse --short=12 HEAD` → goes into tfvars as `deployed_git_sha` (V11).

---

## Phase A1 — The deploy image [PR-1] (~1.5 h)

**Goal:** one additional Dockerfile that produces a self-sufficient image: code + evals + baked model weights + baked `/data`. Proven *offline* on the local machine before AWS exists.

### A1.1 Files to create

**`docker/aws.Dockerfile`** — self-contained (does not `FROM` the api image; repeats its steps so the file reads standalone, then adds the deploy deltas). Structure:

```dockerfile
# Backline AWS deploy image. Build context: repo root, BuildKit required
# (docker/aws.Dockerfile.dockerignore re-admits data/ — see V3).
#   docker build -f docker/aws.Dockerfile -t backline-aws:latest .
# Extends the api image contract with: evals/ (V1), baked HF model weights (V2),
# baked deterministic /data (V3), pre-created trace/eval dirs.
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.8 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1 \
    HF_HOME=/opt/hf

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project --extra embed

COPY backline ./backline
COPY datagen ./datagen
COPY migrations ./migrations
COPY config ./config
COPY evals ./evals
RUN uv sync --frozen --no-dev --extra embed

# Bake the retrieval models so a cold Fargate task never touches Hugging Face (V2).
RUN uv run --no-sync python -c "\
from sentence_transformers import SentenceTransformer, CrossEncoder; \
SentenceTransformer('BAAI/bge-small-en-v1.5'); \
CrossEncoder('cross-encoder/ms-marco-MiniLM-L-6-v2')"

# Bake the deterministic world files: inbox drops (Reconciler runtime input),
# contract PDFs/txt (provenance), writable dirs for traces/eval artifacts (V3, V12).
COPY data /data
RUN mkdir -p /data/traces /data/evals

EXPOSE 8000
CMD ["uv", "run", "--no-sync", "uvicorn", "backline.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

**`docker/aws.Dockerfile.dockerignore`** — BuildKit reads the ignore file that sits next to the named Dockerfile, which is how `data/` gets in *without touching the root `.dockerignore`*:

```
.git
.venv
ui
node_modules
docs
.mypy_cache
.pytest_cache
.ruff_cache
__pycache__
*.py[cod]
.env
data/traces
data/evals
```

(Same exclusions as the root file, minus `data`, plus excluding the two runtime-artifact subdirs so local trace history never ships.)

### A1.2 Build

```bash
# data/ must exist and be the seeded world — A0 pre-flight guarantees it.
docker build -f docker/aws.Dockerfile -t backline-aws:latest .
docker image ls backline-aws   # expect ~2.5–3.5 GB (CPU torch + transformers + models)
```

### A1.3 Gate (binary, offline)

One command; `HF_HUB_OFFLINE=1` proves the weights are truly baked (a network fetch would fail loudly):

```bash
docker run --rm -e HF_HUB_OFFLINE=1 backline-aws:latest \
  uv run --no-sync python -c "\
from sentence_transformers import SentenceTransformer, CrossEncoder; \
SentenceTransformer('BAAI/bge-small-en-v1.5'); \
CrossEncoder('cross-encoder/ms-marco-MiniLM-L-6-v2'); \
import evals, pathlib; \
assert pathlib.Path('/app/evals/results/baseline.json').is_file(), 'baseline missing'; \
n = len(list(pathlib.Path('/data/inbox').glob('*'))); \
assert n == 72, f'inbox drops: {n} != 72'; \
print('A1 GATE: models offline OK · evals importable · baseline present · 72 inbox drops')"
```

Pass = the printed line. Fail = do not proceed; nothing after this works without it.

### A1.4 One comment fix (zero behavior change, allowed by invariant 3)

The stale paragraph at the top of `docker/api.Dockerfile` still claims the `embed` extra is "deliberately NOT installed" while both `uv sync` lines include `--extra embed` (the D-011 re-lock landed; V2). Rewrite that comment to describe reality and point at D-011. A reviewer reading the Dockerfile before the code is exactly who this repo is for; a self-contradicting header is the kind of thing that costs an interview.

### A1.5 PR-1

Branch `claude/aws-deploy-image-*`: the two new docker files, the comment fix, PHASE_LOG entry `A1` recording the gate output verbatim.

---

## Phase A2 — Terraform root module [PR-2] (~1 h writing, ~15 min applying)

**Goal:** `deploy/aws/` — one root module, one file per concern, readable top-to-bottom by a reviewer in four minutes. No premature module extraction: a second environment is when modules earn their keep, and the README says so.

### A2.1 Layout

```
deploy/aws/
  versions.tf              # terraform >= 1.9, aws provider ~> 6.0, region pin
  providers.tf             # provider "aws" { region = var.region, default_tags Project/Ephemeral }
  variables.tf             # every input, typed + described (see A2.2)
  network.tf               # VPC, 2 public subnets, IGW, route table, 3 security groups
  rds.tf                   # db.t4g.micro Postgres 16 + random_password
  ecr.tf                   # 2 repos (api, ui), force_delete = true
  secrets.tf               # 2 Secrets Manager secrets, recovery_window_in_days = 0
  alb.tf                   # ALB, 2 target groups, 2 listeners
  ecs.tf                   # cluster, 3 task definitions (api, ui, eval), 2 services
  iam.tf                   # execution role (+secrets read), EMPTY task role
  logs.tf                  # one CloudWatch log group, 7-day retention
  s3.tf                    # evidence bucket (ALB access logs + dump + eval artifacts)
  outputs.tf               # alb_dns, api_url, ui_url, rds_address, ecr URLs, run-task helper strings
  terraform.tfvars.example # documented example; real tfvars is gitignored
  scripts/
    build_push.sh          # ECR login + build/tag/push both images (reads terraform output)
    run_eval_task.sh       # wraps `aws ecs run-task` for the eval (A5), incl. --resume mode
    fetch_summary.sh       # psql pull of the latest finished eval summary from RDS (A5.4)
  README.md                # the runbook + writeup (grows through A6)
```

`deploy/aws/.gitignore`: `*.tfvars` (except the example), `.terraform/`, `*.tfstate*`, `backline.dump`.

### A2.2 Variables (exact contract)

| var | type | default | notes |
|---|---|---|---|
| `region` | string | `us-west-2` | |
| `home_cidr` | string | — (required) | your `/32`; the only ingress on the ALB and the only non-VPC ingress on RDS |
| `deployed_git_sha` | string | — (required) | 12-char SHA → `GITHUB_SHA` env in task defs (V11) |
| `db_password` | — | *not a variable* | generated by `random_password` (32 chars, no special chars that need URL-escaping: `override_special = "-_"`), lands in state — documented tradeoff, see A2.7 |
| `api_cpu` / `api_memory` | number | 1024 / 8192 | 1 vCPU / 8 GB: lazy-loaded torch + both models + pool headroom (V12) |
| `ui_cpu` / `ui_memory` | number | 256 / 512 | static Next.js standalone server |
| `eval_cpu` / `eval_memory` | number | 2048 / 8192 | concurrency-4 agent loops + reranker on CPU |
| `image_tag` | string | `latest` | both repos |

### A2.3 network.tf — the exact shape

- VPC `10.42.0.0/16`, DNS support+hostnames on.
- Two **public** subnets (`10.42.1.0/24` us-west-2a, `10.42.2.0/24` us-west-2b), `map_public_ip_on_launch = true`. Two AZs because the ALB and the DB subnet group both require two; public because there is **deliberately no NAT Gateway** — tasks get public IPs (`assign_public_ip = ENABLED`) and egress via the IGW for ECR pulls and the Anthropic API. A NAT would add $32/mo + $0.045/GB for zero benefit at this scope; the README says exactly that, because knowing it is the point.
- IGW + one route table (`0.0.0.0/0 → igw`) associated to both subnets.
- Security groups (three, chained — never `0.0.0.0/0` ingress anywhere):
  - `alb_sg`: ingress TCP 80 and 8000 from `var.home_cidr`; egress all.
  - `svc_sg`: ingress TCP 8000 from `alb_sg` and TCP 3000 from `alb_sg` (source_security_group, not CIDR); egress all (Anthropic, ECR, RDS).
  - `rds_sg`: ingress TCP 5432 from `svc_sg` **and** from `var.home_cidr` (the restore path, A3); no other ingress.

### A2.4 rds.tf

```
aws_db_instance "backline":
  engine "postgres", engine_version "16"        # AWS resolves latest 16.x ≥ 16.5 → pgvector 0.8.x (V8)
  instance_class "db.t4g.micro"
  allocated_storage 20, storage_type "gp3", storage_encrypted true
  db_name "backline", username "backline"
  password = random_password.db.result
  db_subnet_group over both public subnets
  vpc_security_group_ids [rds_sg]
  publicly_accessible = true                    # documented tradeoff: /32-locked SG, one-day lifespan,
                                                # enables the from-home restore; production = private
                                                # subnets + SSM tunnel. Comment REQUIRED in the .tf.
  backup_retention_period 0, skip_final_snapshot true, deletion_protection false
  apply_immediately true
```
Default parameter group `default.postgres16` — `rds.force_ssl = 1` stays on; we comply via the DSN (V4). Creation takes ~5–10 min; that's the coffee window.

### A2.5 secrets.tf, iam.tf, logs.tf, s3.tf

- **Secrets** (`recovery_window_in_days = 0`, V13):
  - `backline/database-url` — value **set by Terraform** (`aws_secretsmanager_secret_version`):
    `postgresql://backline:${random_password.db.result}@${aws_db_instance.backline.address}:5432/backline?sslmode=require`
    ⚠️ `.address`, never `.endpoint` — `.endpoint` already contains `:port` and would double it. `?sslmode=require` is load-bearing (V4).
  - `backline/anthropic-api-key` — **secret shell only** in Terraform; the value is set by the human, out of state:
    `aws secretsmanager put-secret-value --secret-id backline/anthropic-api-key --secret-string "$ANTHROPIC_API_KEY"` (run with a leading space so it skips shell history).
- **IAM:**
  - `task_execution_role`: managed `AmazonECSTaskExecutionRolePolicy` + inline `secretsmanager:GetSecretValue` scoped to exactly the two secret ARNs.
  - `task_role`: **empty** — created with zero policies, attached to all three task defs. Backline's agents touch Postgres and Anthropic, never AWS APIs; an empty task role is the least-privilege receipt and one sentence in the writeup.
- **Logs:** one group `/ecs/backline`, retention 7 days; awslogs driver on every container, `awslogs-stream-prefix` = `api` / `ui` / `eval`.
- **S3:** bucket `backline-evidence-<account_id>` (`force_destroy = true`, V13), versioning off, SSE-S3. Uses: ALB access logs (bucket policy for the ELB log-delivery service principal in us-west-2 — Claude Code writes the exact policy), the 17 MB dump (provenance), and post-run eval artifacts. All three uses are real; none are decoration.

### A2.6 alb.tf + ecs.tf

- **ALB** (internet-facing, both subnets, `alb_sg`, access_logs → evidence bucket):
  - TG `backline-api`: port 8000, protocol HTTP, target_type `ip`, health check path **`/readyz`** (proves DB wiring, not just process-up), healthy/unhealthy thresholds 2/3, interval 15s.
  - TG `backline-ui`: port 3000, health check path `/`.
  - Listener `:80` → forward `backline-ui`. Listener `:8000` → forward `backline-api`. Two listeners instead of path-routing because the API serves at root (`/sessions`, `/runs`, `/healthz`) and ALBs don't rewrite paths (V5).
- **ECS:** cluster `backline` (Fargate only, container insights off).
  - Task def `backline-api` — image `<ecr-api>:latest`, cpu/mem from vars, awslogs, env: `DATA_DIR=/data`, `WORLD_SEED=20260805`, `RERANK=on`, `EMBED_MODEL=BAAI/bge-small-en-v1.5`, `RERANK_MODEL=cross-encoder/ms-marco-MiniLM-L-6-v2`, `GITHUB_SHA=${var.deployed_git_sha}`; secrets: `DATABASE_URL`, `ANTHROPIC_API_KEY` (valueFrom the two ARNs).
  - Task def `backline-ui` — image `<ecr-ui>:latest`, no secrets, no env (the API URL is baked at build).
  - Task def `backline-eval` — same image + env + secrets as api, **command override**:
    `["uv","run","--no-sync","python","-m","evals","run","--suite","core","--model","claude-sonnet-5","--budget","20.00","--gate"]`
    plus `EVAL_BUDGET_USD=20.00` in env for belt-and-suspenders. No service wraps this — it runs via `run-task` (V6).
  - Services `api` and `ui`: desired_count 1, launch_type FARGATE, both subnets, `svc_sg`, `assign_public_ip = true`, target group attachments, **`health_check_grace_period_seconds = 300`** — the api image is ~3 GB and a cold pull takes minutes; without the grace period ECS kills healthy-but-still-pulling tasks in a loop.

### A2.7 State honesty (goes in the README verbatim)

`terraform.tfstate` is local, gitignored, and contains `random_password.db` and the composed DATABASE_URL. Accepted for a one-day, one-operator, torn-down-same-day deployment; the production alternatives (S3+DynamoDB remote state with SSE, or `manage_master_user_password = true`) are named in the writeup. Pretending otherwise would be worse than the tradeoff.

### A2.8 Protocol + gate

Claude Code: write all files → `terraform fmt -recursive` → `terraform init` → `terraform validate` → stop.
Human: read `terraform plan` end to end (expect ~35–40 resources, zero destroys), then `terraform apply`.

**Gate:** apply completes; `terraform output` shows `alb_dns`, `rds_address`, both ECR URLs; `aws ecs describe-services` shows both services ACTIVE with 0 running (images not pushed yet — expected, they'll flap until A4 and that's fine); RDS instance `available`.

PR-2 = the whole `deploy/aws/` tree + scripts + README skeleton + PHASE_LOG `A2`.

---

## Phase A3 — Data migration (runbook, ~30 min)

**Goal:** the exact local world — rows, real bge embeddings, ivfflat index, answer key — inside RDS, verified by count and by model tag. The dump/restore is the *experimental control*: DB state held constant means the eval's only variable is the runtime environment (that sentence goes in the writeup).

All commands from WSL2, using the compose db container's own v16 client tools (no host installs, no version skew):

```bash
RDS_URL="postgresql://backline:<password-from-state-or-console>@$(terraform -chdir=deploy/aws output -raw rds_address):5432/backline"

# 1) Dump (local, ~2 s, ~17 MB)
docker compose exec -T db pg_dump -Fc -U backline -d backline > deploy/aws/backline.dump

# 2) Extension first, then restore (psql sslmode=prefer auto-negotiates TLS — V4)
docker compose exec -T db psql "$RDS_URL" -c "CREATE EXTENSION IF NOT EXISTS vector;"
docker compose exec -T db pg_restore --no-owner --no-privileges --no-comments \
  -d "$RDS_URL" < deploy/aws/backline.dump
# --no-comments: the dump's COMMENT ON EXTENSION would fail ownership on RDS; suppressing it
# is standard and harmless. Any other error output = stop and read, do not shrug.

# 3) Verify — numbers must match V7 exactly:
docker compose exec -T db psql "$RDS_URL" -t -c "
  SELECT 'statement_lines', count(*) FROM label.statement_lines
  UNION ALL SELECT 'contract_chunks', count(*) FROM rag.contract_chunks
  UNION ALL SELECT 'expected_ledger', count(*) FROM truth.expected_ledger;"
#  statement_lines | 468160
#  contract_chunks |   2961
#  expected_ledger |   1800
docker compose exec -T db psql "$RDS_URL" -t -c \
  "SELECT DISTINCT embedding_model FROM rag.contract_chunks;"
#  BAAI/bge-small-en-v1.5
docker compose exec -T db psql "$RDS_URL" -t -c "
  SELECT indexname FROM pg_indexes
  WHERE schemaname='rag' AND tablename='contract_chunks'
  AND indexname='contract_chunks_embedding_idx';"
#  contract_chunks_embedding_idx        ← the ivfflat traveled (V8)

# 4) Provenance copy of the dump
aws s3 cp deploy/aws/backline.dump s3://backline-evidence-<acct>/migration/backline.dump
```

**Gate:** all three counts + model tag + index name exact. `migrate` is *not* run (V8) — note that in the README as a deliberate no-op with the reason.

---

## Phase A4 — Push, stabilize, smoke (~1–1.5 h, mostly waiting on uploads/pulls)

```bash
# scripts/build_push.sh does all of this; shown expanded for the runbook:
ALB=$(terraform -chdir=deploy/aws output -raw alb_dns)
aws ecr get-login-password --region us-west-2 | docker login --username AWS --password-stdin <acct>.dkr.ecr.us-west-2.amazonaws.com

# API image — already built and gated in A1; just tag+push (~3 GB first push)
docker tag backline-aws:latest <ecr-api>:latest && docker push <ecr-api>:latest

# UI image — built NOW, because NEXT_PUBLIC_API_URL bakes at build time (V5)
docker build -f ui/Dockerfile --build-arg NEXT_PUBLIC_API_URL="http://${ALB}:8000" -t backline-ui-aws:latest ui/
docker tag backline-ui-aws:latest <ecr-ui>:latest && docker push <ecr-ui>:latest

aws ecs update-service --cluster backline --service api --force-new-deployment
aws ecs update-service --cluster backline --service ui  --force-new-deployment
aws logs tail /ecs/backline --follow    # watch both come up
```

**Smoke sequence (in order, each gates the next):**
1. `curl -s http://${ALB}:8000/healthz` → `{"status":"ok",...}` (process up)
2. `curl -s http://${ALB}:8000/readyz` → `{"status":"ok","database":"ok"}` — **this is the SSL + SG + restore proof in one line**
3. Browser (from home, only place it works): `http://<ALB>` → UI loads, **no demo-mode badge** (key injected → live providers)
4. One live chat question (e.g. ask Counsel a deal-terms question) → answer with clause citations; open the Trace Inspector, watch the spans. First query pays the one-time model load (V12) — seconds, once.
5. Eval Dashboard loads and shows the *local* run history (it's reading restored RDS rows) and the committed baseline (V1's COPY at work).

**Failure routing:** 2 fails with `database "unreachable"` → SG chain or missing `?sslmode=require` (read the detail string — it names which). 3 shows demo badge → the key secret is empty or the exec role can't read it (check task stopped-reason / logs). Tasks loop STOPPED on pull → grace period misconfigured or ECR URL typo'd.

---

## Phase A5 — The paired eval (the actual point) (~1.5–2 h wall, ~$16–20 API)

**Design (V10):** two fresh full runs, same day, identical flags, identical DB state — one on the local rig (control), one as a Fargate task (treatment). Judged against the repo's own documented noise floor. The committed sweep row 62865d3c (91.6 overall) stands as a third reference point. Pre-registered, before either run starts:

> **Hypothesis:** |AWS overall − local overall| ≤ 3.0 points (BENCHMARK_NOTES §5.4 noise bound), with no category diverging in a way that survives its small-n arithmetic (reconciliation is F1 with double-digit swing per flag; abstention is n=10 → ±10/question; adversarial n=3 → ±11/check).

### A5.1 Local control

```bash
uv run python -m evals run --suite core --model claude-sonnet-5 --budget 20.00
# projection ≈ $11.27 (intro pricing) < 20.00 → runner proceeds without --yes (V10)
# actual spend ≈ $8.09 per the committed sweep receipts; ~30–45 min at concurrency 4
```

### A5.2 AWS treatment

```bash
deploy/aws/scripts/run_eval_task.sh          # wraps:
aws ecs run-task --cluster backline --launch-type FARGATE \
  --task-definition backline-eval \
  --network-configuration "awsvpcConfiguration={subnets=[<a>,<b>],securityGroups=[<svc_sg>],assignPublicIp=ENABLED}"
aws logs tail /ecs/backline --follow --log-stream-name-prefix eval
```
The task exits with the gate's exit code (`--gate` in the command). The rendered summary table prints to CloudWatch — screenshot it there: an eval suite grading agents in CloudWatch Logs is exactly the picture this project exists to produce.

### A5.3 The heal loop (expected, not exceptional)

Mid-run 429/529s produce quarantined infra errors; the gate refuses them by design (D-032). If `errors.n > 0`:
```bash
RUN_ID=$(docker compose exec -T db psql "$RDS_URL" -t -A -c \
  "SELECT id FROM app.eval_runs ORDER BY started_at DESC LIMIT 1")
deploy/aws/scripts/run_eval_task.sh --resume "$RUN_ID" --retry-errors
# (script appends ["--resume","<id>","--retry-errors"] to the container command override)
```
Legitimately-scored rows are never touched; the run heals in place. Same command works locally for the control run.

### A5.4 Artifact extraction (Fargate disk is gone; RDS is not — V6)

```bash
deploy/aws/scripts/fetch_summary.sh          # wraps:
docker compose exec -T db psql "$RDS_URL" -t -A -c \
  "SELECT summary FROM app.eval_runs WHERE finished_at IS NOT NULL \
   ORDER BY started_at DESC LIMIT 1" > deploy/aws/evidence/aws-run-summary.json
uv run python -m evals report --summary deploy/aws/evidence/aws-run-summary.json
uv run python -m evals gate   --summary deploy/aws/evidence/aws-run-summary.json   # strict check, on the record
aws s3 cp deploy/aws/evidence/ s3://backline-evidence-<acct>/evals/ --recursive
```

### A5.5 Reading the result (write this before knowing it)

- **Within noise (expected):** the claim is *"the platform's measured behavior is environment-invariant: a 133-question exact-ground-truth suite scored within the pre-registered same-model noise bound across my homelab and ECS/RDS, with DB state held constant by migration."*
- **Strict gate FAILs on the AWS run:** report it with its reason. A T2-violation flicker (live rows showed 3–7) or a small-n category swing is *variance with a paper trail* — cite §5.4 and the adjudication precedent (the benchmark's T2 miss adjudicated as a checker false positive). That's not spin; it's the repo's documented epistemics applied to one more run.
- **Outside noise (> ~3 overall or a structural category shift):** that's a *finding*, and findings are the brand. Chase it in the traces (latency-driven tool timeouts on Fargate CPU are the first suspect — `TOOL_TIMEOUT_S=30` vs a reranker on 2 vCPU), write it up honestly, and the deploy repo just became more interesting than a clean pass.

Deliverable: a three-column parity table (local control · AWS treatment · committed row 62865d3c) per category + overall + $/query + p50/p95, in `deploy/aws/README.md`.

---

## Phase A6 — Evidence, writeup, teardown [PR-3] (~1–1.5 h + 10 min destroy)

### A6.1 Capture before destroying (nothing survives the destroy except the repo and S3)

- [ ] UI over the ALB: chat with citations, Trace Inspector on the AWS eval run, Eval Dashboard showing the AWS row next to local history, Review Queue.
- [ ] CloudWatch: the eval task's rendered summary table.
- [ ] ECS console: both services healthy; the eval task's STOPPED entry with exit code.
- [ ] RDS console: instance page (encrypted, 16.x, t4g.micro).
- [ ] `terraform output`, `aws ecs describe-services` snippets → `deploy/aws/evidence/`.
- [ ] Final cost: Billing → by-service, screenshot the day's actuals next to §8's estimate. An estimate-vs-actual table is rarer than it should be.

### A6.2 The writeup (`deploy/aws/README.md`, ~150 lines, structure fixed now)

1. **What this is** (3 sentences) + architecture sketch (ALB two-listener → api/ui on Fargate → RDS; eval as run-task).
2. **The parity table** + the pre-registered hypothesis and verdict.
3. **Decisions with reasons** — no NAT (and what it saves), two listeners vs path routing, public RDS + /32 (and the production alternative), empty task role, force_ssl compliance via DSN, dump/restore as experimental control, state tradeoff (A2.7).
4. **What broke** — every real error hit during the day, verbatim, with the fix. This section is written *during* the day, not reconstructed after. It is the credibility engine of the whole artifact.
5. **What production adds** — private subnets + endpoints/NAT, ACM+HTTPS, remote state, CI-driven pushes, autoscaling, RDS backups/Multi-AZ. Named, not built, on purpose.
6. **Reproduce it** — the runbook distilled to ~15 commands.

Root `README.md` gains a short "Deployed on AWS" section linking to it (with the parity table's one-line verdict). `CLAUDE.md` gains an AWS appendix: the invariants from §0.2, "never apply/destroy," and where deploy files live.

### A6.3 Teardown

```bash
terraform -chdir=deploy/aws destroy      # human runs it; ~10 min
```
Pre-solved by V13 (`force_delete`, `force_destroy`, `recovery_window_in_days = 0`): one clean pass, no orphans. Afterward: `aws resourcegroupstaggingapi get-resources --tag-filters Key=Project,Values=backline` → empty list = proof; screenshot it. Check Billing the next morning once; the Budget alert is the backstop.

PR-3 = evidence, parity table, both READMEs, CLAUDE.md amendment, PHASE_LOG `A3–A6`.

---

## 8. Cost (verified rates, V14)

| Item | Rate | Day (~12 h live) |
|---|---|---|
| Fargate api (1 vCPU / 8 GB) | $0.0760/hr | $0.91 |
| Fargate ui (0.25 vCPU / 0.5 GB) | $0.0123/hr | $0.15 |
| Fargate eval task (2 vCPU / 8 GB, ~1.5 h incl. heal) | $0.1165/hr | $0.17 |
| ALB (2 listeners, trickle traffic) | ~$0.0225/hr + LCU | $0.35 |
| RDS db.t4g.micro + 20 GB gp3 | ~$0.016/hr + storage | $0.25 |
| ECR (~3.5 GB), S3, Secrets ×2, CloudWatch, data transfer | — | ~$0.35 |
| **Infra total** | | **≈ $2.20** (≤ $5 even at 24 h) |
| Eval API spend: local control + AWS treatment + heals | $8.09 × 2 + slack | **≈ $16–20** |
| **Day, all-in** | | **≈ $20–25** |

Leaving it up costs ≈ $3.20/day. Don't; the artifact is the repo.

## 9. Failure playbook (fastest fix first)

| Symptom | Cause | Fix |
|---|---|---|
| `readyz` 503 `database unreachable`, detail mentions `pg_hba`/SSL | missing `?sslmode=require` (V4) | fix the secret value, force new deployment |
| `readyz` 503, detail is a timeout | SG chain (rds_sg must source `svc_sg`) | fix SG, apply — no redeploy needed |
| Everything times out from the browser | home IP rotated | `curl ifconfig.me` → tfvars → `terraform apply` (20 s) |
| Tasks loop STOPPED, `CannotPullContainerError` | ECR URL/tag typo, or no public IP on the task | check task def image string; `assignPublicIp=ENABLED` |
| Tasks killed at ~2 min while pulling | missing `health_check_grace_period_seconds=300` | it's in A2.6; if dropped, add and apply |
| UI loads, API calls fail in console | UI built before ALB existed / wrong `NEXT_PUBLIC_API_URL` | rebuild UI image with `http://<alb>:8000`, push, redeploy |
| Demo-mode badge on AWS | key secret empty or exec role can't read it | `put-secret-value`, check exec-role inline policy, redeploy |
| Eval task refuses at start, `refused: projection …` | budget below projection | keep `--budget 20.00`; if pricing tier flipped (post-2026-08-31), raise it consciously — never `--yes` reflexively |
| `errors.n > 0` in summary / gate fails on quarantine | provider 429/529 mid-run | the heal loop, A5.3 — designed for exactly this (D-032) |
| Gate fails on `t2_violations` or a small-n category | documented live-run variance (V10) | report per A5.5; do not re-roll until it passes — one heal pass, then publish what happened |
| Slow first chat answer after deploy | lazy model load (V12) | not a bug; note it and move on |
| `terraform destroy` sticks on ECR/S3/secret | V13 flags missing | they're specced in A2; if dropped, add + re-destroy |

## 10. Run order for the day (~7–9 h)

```
Night before  A0 checklist (45 min)
09:00  A1  image + offline gate                    [PR-1]
10:30  A2  terraform written → human plan+apply    [PR-2]  (RDS ≈ 10 min = the wait)
11:15  A3  dump → restore → verify counts
11:45  A4  push images, services green, smoke 1–5
13:00  A5  local control run  ──┐  start both, watch logs
13:10  A5  AWS eval task      ──┘  heal if needed, extract, parity table
15:00  A6  evidence, writeup, README/CLAUDE.md     [PR-3]
16:30  terraform destroy · tag-scan proof · done
```

The clock assumes prep actually happened the night before and that you stay at the keyboard during applies and pushes — the human is the feedback loop (§0.3). If the day slips, the only safe overnight state is *destroyed*; everything needed to resume is in the repo and S3.
