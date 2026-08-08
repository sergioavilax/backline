# Backline on AWS

Backline — an agentic royalty-operations platform with a 133-question
exact-ground-truth eval suite — deployed onto AWS with Terraform, its exact local
world migrated into RDS, and the full suite re-run *on AWS infrastructure* to test
whether the measured behaviour survives the change of environment.

The artifact is this directory, the parity table, and the "what broke" log. Not a
running service: everything here was torn down the same day, on purpose.

> **Status: Phase A2 complete.** The Terraform tree is written, `init`/`validate`
> clean, and applied. Sections marked _(pending)_ below are filled in as A3–A6
> execute. Nothing in this README is written ahead of the evidence.

---

## Architecture

```
                    ┌──────────────── your home IP /32 ────────────────┐
                    │                                                  │
                    ▼                                                  ▼
              ALB :80  ──────────► backline-ui  (Fargate, 0.25 vCPU)
              ALB :8000 ─────────► backline-api (Fargate, 1 vCPU / 8 GB)
                    │                     │
                    │                     ▼
                    │              RDS Postgres 16 + pgvector
                    │              (db.t4g.micro, force_ssl)
                    │                     ▲
                    └── backline-eval ────┘   one-off `aws ecs run-task`
                        (2 vCPU / 8 GB)       no service, no load balancer
```

Three security groups, chained: the internet reaches only the ALB, the ALB reaches
only the services, the services reach only the database. There is no `0.0.0.0/0`
ingress anywhere in `network.tf`.

## Layout

| File | What lives there |
|---|---|
| `versions.tf` | Terraform ≥ 1.9, AWS provider ~> 6.0 |
| `providers.tf` | Region pin, `default_tags` (`Project`, `Ephemeral`) |
| `variables.tf` | Every input, typed and described |
| `network.tf` | VPC, 2 public subnets, IGW, route table, the 3-SG chain |
| `rds.tf` | `db.t4g.micro` Postgres 16, `random_password` |
| `ecr.tf` | Two repositories, `force_delete = true` |
| `secrets.tf` | Two secrets, `recovery_window_in_days = 0` |
| `alb.tf` | ALB, two target groups, two listeners |
| `ecs.tf` | Cluster, three task definitions, two services |
| `iam.tf` | Execution role (+ scoped secrets read), **empty** task role |
| `logs.tf` | One log group, 7-day retention |
| `s3.tf` | Evidence bucket (ALB logs · dump · eval artifacts) |
| `outputs.tf` | Everything the runbook reads |
| `scripts/` | `build_push.sh`, `run_eval_task.sh`, `fetch_summary.sh` |

One root module, one file per concern, no submodules. A second environment is when
modules start earning their keep; there is exactly one here, and premature module
extraction would make this tree harder to read for zero benefit.

## Decisions with reasons

**No NAT Gateway.** Tasks run in public subnets with public IPs and egress through
the IGW. A NAT would add ~$32/month plus $0.045/GB to reach ECR and the Anthropic
API — the only two things these tasks talk to outbound. At this scope and lifespan
it buys nothing. Production, with private subnets, would use VPC endpoints for ECR
and a NAT for the Anthropic egress.

**Two listeners, not path routing.** The API serves at the root of its own
namespace (`/sessions`, `/runs`, `/healthz`), and an ALB forwards paths verbatim
rather than rewriting them — a `/api/*` rule would deliver `/api/sessions` to a
server that only knows `/sessions`. Splitting by port gives both services one
stable DNS name with no domain and no certificate, which is what makes the UI
build possible at all: `NEXT_PUBLIC_API_URL` is baked into the Next.js bundle at
build time, so the API's address has to be stable *before* the UI image exists.

**Public RDS behind a /32.** The instance is publicly reachable so the migration
can run from the operator's laptop straight into RDS. What makes that defensible
is the security group: 5432 is open to exactly one `/32` and to the service SG,
nothing else — plus a lifespan measured in hours. Production puts the database in
private subnets and reaches it through an SSM tunnel; that costs an extra hop and
a NAT or endpoint, which buys nothing for a one-day exercise.

**Empty task role.** Backline's agents talk to Postgres and to Anthropic. They
call no AWS API at all. The role the application code can actually reach therefore
has zero policies attached — the cheapest possible proof of least privilege, since
there is no policy to audit. The *execution* role (used by the ECS agent, not by
app code) holds the image pull, the log write, and a `GetSecretValue` scoped to
exactly two secret ARNs.

**Complying with `force_ssl` rather than disabling it.** RDS PostgreSQL 15+ sets
`rds.force_ssl = 1` in the default parameter group. Instead of creating a custom
parameter group to turn that off, the composed DSN carries `?sslmode=require`,
which asyncpg parses and negotiates without any code change. Zero extra resources,
and the better story.

**Dump/restore as the experimental control.** The AWS eval is only interesting if
the database is not a variable. Migrating the exact local world — same rows, same
real bge embeddings, same ivfflat index, same answer key — holds DB state constant
so the only thing that changes between the two runs is the runtime environment.

## State honesty

`terraform.tfstate` is local, gitignored, and contains `random_password.db` and
the composed `DATABASE_URL`. That is accepted for a one-day, one-operator,
torn-down-same-day deployment. The production alternatives are real and named
rather than implied: S3 remote state with SSE and DynamoDB locking, or
`manage_master_user_password = true` to hand the credential to Secrets Manager and
keep it out of state entirely. Pretending the tradeoff was not made would be worse
than the tradeoff.

The Anthropic API key is *not* in state. Terraform creates an empty secret shell
and the operator sets the value out of band, so the key never enters the repo, the
state file, or any plan output.

## Runbook

Prerequisites: the A0 checklist, and the A1 image built and gated
(`docker build -f docker/aws.Dockerfile -t backline-aws:latest .`).

```bash
cd deploy/aws
cp terraform.tfvars.example terraform.tfvars   # fill in home_cidr + deployed_git_sha

terraform init
terraform validate
terraform plan                                  # read it end to end
terraform apply                                 # ~10 min, RDS is the long pole
```

Then, once:

```bash
 aws secretsmanager put-secret-value \
   --secret-id backline/anthropic-api-key \
   --secret-string "$ANTHROPIC_API_KEY"
```

(leading space — it keeps the command out of shell history)

```bash
# A3 — migrate the world
RDS_URL="$(terraform -chdir=deploy/aws output -raw database_url)"
docker compose exec -T db pg_dump -Fc -U backline -d backline > deploy/aws/backline.dump
docker compose exec -T db psql "$RDS_URL" -c "CREATE EXTENSION IF NOT EXISTS vector;"
docker compose exec -T db pg_restore --no-owner --no-privileges --no-comments \
  -d "$RDS_URL" < deploy/aws/backline.dump

# A4 — push images, stabilise, smoke
deploy/aws/scripts/build_push.sh

# A5 — the paired eval
uv run python -m evals run --suite core --model claude-sonnet-5 --budget 20.00  # local control
deploy/aws/scripts/run_eval_task.sh                                             # AWS treatment
deploy/aws/scripts/fetch_summary.sh                                             # artifacts out of RDS

# A6 — teardown
terraform -chdir=deploy/aws destroy
aws resourcegroupstaggingapi get-resources --tag-filters Key=Project,Values=backline
```

`python -m backline.db.migrate` is deliberately **not** run against RDS. The dump
includes `schema_migrations`, so migrating would be a no-op; the ivfflat index and
its `ANALYZE` travel inside the dump as well, so there is no re-embed and no
re-index either.

## Migration verification _(pending — A3)_

## Parity table _(pending — A5)_

## What broke _(pending — written during the day, not reconstructed after)_

## What production would add

Private subnets with VPC endpoints for ECR/Secrets Manager and a NAT for outbound
Anthropic traffic · ACM certificate and an HTTPS listener with a real domain ·
remote state in S3 with locking · image builds and pushes driven by CI rather than
a laptop · service autoscaling on ALB request count · RDS automated backups,
Multi-AZ, and a maintenance window · CloudWatch alarms and a dashboard rather than
`logs tail`. Each is named and not built on purpose: this deployment's job is to
be read, run once, and destroyed.

## Cost

Roughly **$2.20** of infrastructure for a ~12-hour day (Fargate $1.23 · ALB $0.35 ·
RDS $0.25 · ECR/S3/Secrets/CloudWatch ~$0.35), plus the eval API spend of the two
runs. Leaving it up would cost about $3.20/day — don't; the artifact is the repo.
Estimate-versus-actual lands here after teardown _(pending — A6)_.
