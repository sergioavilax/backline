# Backline on AWS

Backline is an agentic royalty-operations platform with a 133-question,
exact-ground-truth eval suite. This directory deployed it onto AWS with Terraform,
migrated the exact local world into RDS, and re-ran the full suite *on AWS
infrastructure* as a one-off Fargate task to test whether the platform's measured
behaviour survives a change of environment. The answer was yes, to within 0.8
points — and the interesting part is how that number had to be read.

The artifact is this directory: the Terraform, the parity table, and the honest
account of what happened. Not a running service. Everything was destroyed the same
day, on purpose.

---

## Architecture

```
                    ┌──────────────── operator home IP /32 ────────────────┐
                    │                                                      │
                    ▼                                                      ▼
              ALB :80  ──────────► backline-ui   (Fargate, 0.25 vCPU / 0.5 GB)
              ALB :8000 ─────────► backline-api  (Fargate, 1 vCPU / 8 GB)
                                          │
                                          ▼
                                   RDS Postgres 16.13 + pgvector 0.8.x
                                   (db.t4g.micro, encrypted, force_ssl)
                                          ▲
                    backline-eval ────────┘   one-off `aws ecs run-task`
                    (2 vCPU / 8 GB)           no service, no load balancer
```

38 resources, one region (`us-west-2`), one root module. Three security groups
chained so the internet reaches only the ALB, the ALB reaches only the services,
and the services reach only the database — **there is no `0.0.0.0/0` ingress
anywhere in `network.tf`.** Every ingress is either the operator's `/32` or another
security group by id.

---

## The parity result

**Design.** Two fresh full runs, same day, identical flags
(`--suite core --model claude-sonnet-5 --budget 20.00`, concurrency 4, default
judge), identical database state, identical suite hash (`6eef41c6706f309a`) and
identical judge rubric (`ffe8c9753172`). One on the local rig (control), one as a
Fargate task (treatment). The dump/restore is what makes this an experiment rather
than an anecdote: holding DB state constant means the only variable is the runtime
environment.

**Pre-registered hypothesis**, written before either run started:

> |AWS overall − local overall| ≤ 3.0 points — the same-model/same-suite noise
> bound established in `docs/BENCHMARK_NOTES.md` §5.4 — with no category diverging
> in a way that survives its small-n arithmetic.

### Three columns, one suite

| category | n | baseline | local control | **AWS treatment** | sweep `62865d3c` | AWS − local |
|---|---:|---:|---:|---:|---:|---:|
| catalog_lookup | 15 | 100.0 | 100.0 | 100.0 | 100.0 | 0.0 |
| royalty_math | 25 | 100.0 | 100.0 | 100.0 | 100.0 | 0.0 |
| recoupment_state | 15 | 100.0 | 100.0 | 100.0 | 100.0 | 0.0 |
| cross_collateral | 8 | 100.0 | 100.0 | 100.0 | 100.0 | 0.0 |
| sql_analytics | 10 | 100.0 | 100.0 | 100.0 | 100.0 | 0.0 |
| adversarial | 3 | 93.3 | 100.0 | 100.0 | 100.0 | 0.0 |
| abstention | 10 | 100.0 | 90.0 | **100.0** | 90.0 | **+10.0** |
| multi_step | 12 | 72.8 | 65.0 | **72.2** | 67.8 | **+7.2** |
| contract_terms | 20 | 85.0 | 82.7 | **77.0** | 81.0 | **−5.7** |
| reconciliation | 15 | 96.7 | 98.3 | **86.7** | 83.3 | **−11.7** |
| **overall** | **133** | **94.8** | **93.3** | **92.5** | **91.6** | **−0.8** |
| | | | | | | |
| spend | | | $7.880494 | $8.007034 | $8.094698 | |
| $/query (incl. judge) | | | $0.0593 | $0.0602 | $0.0609 | |
| latency p50 | | | 12,678 ms | **12,508 ms** | 13,033 ms | −170 ms |
| latency p95 | | | 65,855 ms | 71,122 ms | 75,870 ms | +5,267 ms |
| T2 violations | | | 2 | 7 | 3 | |
| quarantined infra errors | | | **0** | **0** | 0 | |
| git sha | | | `306f13d886a9` | `01f6febe8675` | `885505aa01ea` | |

Run ids: local `a309dc57-b68e-4fbd-8591-38c2e7c63263` · AWS
`93731060-f3f8-48d3-aa24-80bccf773733` · sweep
`62865d3c-ee64-4801-bc6d-c22543e17b1b`.

### Verdict: hypothesis CONFIRMED at Δ 0.8

|92.5 − 93.3| = **0.8 points**, against a pre-registered bound of 3.0 and a
documented same-model spread of 3.2. Both runs completed all 133 questions with
**zero quarantined infra errors**, so no heal pass was needed and no row is
carrying an outage in a model costume.

The claim this supports, stated exactly as narrowly as the evidence allows:

> The platform's measured behaviour is environment-invariant. A 133-question
> exact-ground-truth suite scored within the pre-registered same-model noise bound
> across a homelab and ECS/RDS, with database state held constant by migration.

### Provenance note on the AWS image

The AWS run reports git `01f6febe8675` and the local control `306f13d886a9`. Those
are different commits, and the difference is *only* deploy scaffolding: `01f6feb`
is the PR-1 merge (the deploy image), and `306f13d` adds PR-2's `deploy/aws/`
Terraform tree. **No runtime Python, SQL, prompt, or config differs between them** —
PR-2 touched `deploy/**` and `docs/PHASE_LOG.md` and nothing else. The image was
built once at PR-1 state and pushed unchanged; the suite hash and judge rubric are
byte-identical across both runs. The comparison is therefore same-code, and the
differing SHAs are attribution metadata, not a confound.

### The strict gate FAILED — on both runs, in different places

This is the part worth reading carefully, because the honest reading is not the
convenient one.

```
$ python -m evals gate --summary deploy/aws/evidence/aws-run-summary.json
gate: FAIL
  ✗ contract_terms: 77.0 vs baseline 85.0 (-8.0 pts > 3)
  ✗ reconciliation: 86.7 vs baseline 96.7 (-10.0 pts > 3)
  ✗ 7 T2 violation(s) — process assertions failed
  · adversarial: improved 93.3 → 100.0
```

The strict gate compares a run against the composite baseline and fails any
category down more than 3 points. It failed the AWS run. It also failed the local
control:

```
$ python -m evals gate --summary data/evals/a309dc57-.../summary.json
gate: FAIL
  ✗ abstention: 90.0 vs baseline 100.0 (-10.0 pts > 3)
  ✗ multi_step: 65.0 vs baseline 72.8 (-7.8 pts > 3)
  ✗ 2 T2 violation(s) — process assertions failed
  · adversarial: improved 93.3 → 100.0
  · reconciliation: improved 96.7 → 98.3
```

**Both fresh runs fail the strict gate, on entirely disjoint categories.** That is
the cleanest available evidence that the failures are variance rather than an AWS
problem. A broken environment produces a systematic deficit — the same categories
degrading, in the same direction, for a traceable mechanical reason. What actually
happened is scatter in *both* directions: AWS lost 5.7 on contract_terms and 11.7
on reconciliation, and *gained* 10.0 on abstention and 7.2 on multi_step. Noise
scatters; breakage accumulates.

The per-category mechanics were pre-registered in BENCHMARK_NOTES §9 and calibrated
in §5.4, and they are exactly what this table shows:

- **reconciliation is F1-scored**, so a couple of flag misses swing the category by
  double digits. §5.4's own same-model pair moved it 96.7 → 83.3 (−1.51 weighted
  overall) — the single largest contributor to the documented 3.2-point spread.
  The AWS run's 86.7 sits *between* the baseline and that historical fresh run, and
  **beats the committed sweep row's 83.3**. Read against the fresh-run reference
  rather than the composite, the local control's 98.3 is the outlier roll, not the
  AWS run's 86.7.
- **abstention is n=10**, so one question is ±10 points. Baseline 100, sweep 90,
  local 90, AWS 100 — the AWS run matched the baseline here and the local control
  did not.
- **adversarial is n=3**, so one T2 check is ±11. Both runs scored 100.0, *above*
  the 93.3 baseline.
- **T2 violations ranged 3–7 across historical live rows.** The AWS run's 7 is at
  the top of that observed range, the local control's 2 just below it. There is
  precedent for adjudicating these individually: the Phase 7 close-out resolved an
  opus T2 miss as a *checker* false positive — a naive negation-unaware substring
  match — with no model fault. Nothing here was re-adjudicated; the counts are
  published as measured.

Per §A5.5 this is reported, not re-rolled. One heal pass was available and not
needed (zero infra errors). No run was repeated to get a nicer number, and both
runs publish whatever they said.

**What would have falsified the hypothesis**, stated so the claim is not
unfalsifiable: an overall gap > 3.0; or a systematic one-directional degradation;
or `TOOL_TIMEOUT_S` exhaustion on the reranker under 2 vCPU showing up as
correlated failures in the retrieval-heavy categories. The first did not happen
(Δ 0.8). The second did not happen (gains and losses both). The third did not
happen — p50 latency on AWS was actually **170 ms faster** than local, with p95
5.3 s slower, which is a tail effect and not a timeout wall.

---

## Evidence

Every number in the parity table above is reproducible from the two summary files
below — they are the inputs, not illustrations of the output.

| File | What it shows |
|---|---|
| [`evidence/aws-run-summary.json`](evidence/aws-run-summary.json) | The AWS run's summary row, pulled from `app.eval_runs` in RDS |
| [`evidence/local-control-summary.json`](evidence/local-control-summary.json) | The local control run's summary — the other half of the parity table |
| [`evidence/evals-dashboard-aws-run.png`](evidence/evals-dashboard-aws-run.png) | The deployed Eval Dashboard showing the AWS run beside local history |
| [`evidence/trace-inspector-aws-eval.png`](evidence/trace-inspector-aws-eval.png) | The Trace Inspector on the AWS eval run — spans, tokens, cost |
| [`evidence/cloudwatch-gate-output.png`](evidence/cloudwatch-gate-output.png) | The rendered summary table and gate verdict in CloudWatch Logs |
| [`evidence/ecs-eval-task-stopped.png`](evidence/ecs-eval-task-stopped.png) | The stopped eval task: 42 s pull, 11 m 48 s runtime, exit code 1 |
| [`evidence/billing-day-of.png`](evidence/billing-day-of.png) | Billing console, day of — account-wide and pre-lag; see [Cost](#cost-estimate-vs-actual) for why it is not the actual |
| [`evidence/teardown-proof.txt`](evidence/teardown-proof.txt) | Post-destroy verification: the tag scan and the ten per-service checks that supersede it |

Reproduce either run's table from its summary:

```bash
uv run python -m evals report --summary deploy/aws/evidence/local-control-summary.json
uv run python -m evals report --summary deploy/aws/evidence/aws-run-summary.json
```

**This directory is the only surviving copy.** These artifacts were also archived to
`s3://backline-evidence-675362625117/evals/` during A5–A6, alongside the 24 MB
migration dump — and `terraform destroy` deleted that bucket, because it is a
Terraform-managed resource with `force_destroy = true`. `aws s3 ls` on it now
returns `NoSuchBucket`. Nothing was lost, but only because these files were
committed here too; see [Teardown lesson 3](#teardown-and-three-things-it-taught).

**The ECS lifecycle, read off the console:** image pull completed in **42 seconds**
for a ~4 GB image — the baked model weights earning their place, since a cold
Hugging Face fetch at task start would have added minutes and a network dependency.
The task ran **11 m 48 s** and exited **1**. That exit code is not a failure of the
deployment: `--gate` is in the command override, so the container's exit status
*is* the gate's verdict, surfaced where an operator would look for it. A green
deployment that exits non-zero because the thing it measured did not meet a
threshold is the system working.

---

## Migration verification (A3)

The dump/restore is the experimental control. Verified against RDS after
`pg_restore`, matching the pre-migration local counts exactly:

| check | expected | RDS |
|---|---:|---:|
| `label.statement_lines` | 468,160 | 468,160 |
| `rag.contract_chunks` | 2,961 | 2,961 |
| `truth.expected_ledger` | 1,800 | 1,800 |
| `DISTINCT embedding_model` | `BAAI/bge-small-en-v1.5` | `BAAI/bge-small-en-v1.5` |
| ivfflat index | `contract_chunks_embedding_idx` | present |

The embedding-model check is not ceremony. Query-time search reads the
`embedding_model` recorded on the stored chunks and must embed queries with the
same model; a mismatch does not raise, it silently returns wrong neighbours — the
worst possible failure mode for a retrieval eval. A stale `hash-bow-384-v1` store
would have produced a plausible-looking run with meaningless retrieval.

`python -m backline.db.migrate` was deliberately **not** run against RDS. The dump
carries `schema_migrations`, so migrating would be a no-op; the ivfflat index and
its `ANALYZE` travel inside the dump too, so there was no re-embed and no re-index.
The dump was archived to
`s3://backline-evidence-675362625117/migration/backline.dump` as provenance for
exactly this claim — and then `terraform destroy` deleted that bucket along with the
rest of the stack. The dump is not in git either (24 MB, gitignored). What survives
is this verification table and the counts in the A3 PHASE_LOG entry; see
[Teardown lesson 3](#teardown-and-three-things-it-taught) for why the archive was
inside the blast radius of the teardown, which is a design error worth naming.

**One §0.4 number moved.** V7 measured `pg_dump -Fc` at 17 MB; the actual dump was
**24 MB** (and the database 136 MB against V7's 130 MB). The cause is measured, not
guessed: V7 profiled a *freshly seeded* cold sandbox, whereas this database had
accumulated **11 `app.eval_runs` and 1,447 `app.eval_results`** rows of real eval
history by the time it was dumped — including the Phase 7 sweep and the A5 local
control. Nothing about the restore or the verification changed; the world tables
are byte-for-byte what V7 described, and the extra 7 MB is the answer key's
*results* history riding along, not the answer key itself.

---

## Decisions with reasons

**No NAT Gateway.** Tasks run in public subnets with public IPs and egress through
the IGW. A NAT would add ~$32/month plus $0.045/GB to reach the only two outbound
destinations these tasks have — ECR and the Anthropic API. At this scope it buys
nothing. Production, with private subnets, would use VPC endpoints for ECR and
Secrets Manager and a NAT for the Anthropic egress.

**Two listeners, not path routing.** The API serves at the root of its own
namespace (`/sessions`, `/runs`, `/healthz`), and an ALB forwards paths verbatim
rather than rewriting them — a `/api/*` rule would deliver `/api/sessions` to a
server that only knows `/sessions`. Splitting by port gives both services one
stable DNS name with no domain and no certificate. That stability is load-bearing:
`NEXT_PUBLIC_API_URL` is baked into the Next.js bundle at build time, so the API's
address must exist and be fixed *before* the UI image is built — which is why the
UI image is built after `terraform apply`, not before.

**Public RDS behind a /32.** The instance is publicly reachable so the migration
could run from the operator's laptop straight into RDS. What makes that defensible
is the security group — 5432 open to exactly one `/32` and to the service SG,
nothing else — plus a lifespan measured in hours. Production puts the database in
private subnets behind an SSM tunnel; that costs an extra hop and a NAT or
endpoint, which buys nothing for a one-day exercise. Stated rather than hidden,
because hiding it would be the actual mistake.

**Empty task role.** Backline's agents talk to Postgres and to Anthropic and call
no AWS API at all. So the role the *application code* can reach was created with
zero policies and stayed that way — verified post-apply, not asserted:
`list-attached-role-policies` and `list-role-policies` both return `[]`. It is the
cheapest possible least-privilege receipt: there is no policy to audit because
there is no policy. The *execution* role (used by the ECS agent, not app code)
holds the image pull, the log write, and a `GetSecretValue` scoped to exactly two
secret ARNs.

**Complying with `force_ssl` rather than disabling it.** RDS PostgreSQL 15+ sets
`rds.force_ssl = 1` in the default parameter group. Rather than create a custom
parameter group to switch it off, the composed DSN carries `?sslmode=require`,
which asyncpg parses and negotiates with no code change. Zero extra resources, and
the better story.

**The key never touches Terraform.** `backline/anthropic-api-key` is created as an
empty secret *shell*; the operator sets the value out of band with a
leading-space CLI call. The key is therefore absent from the repo, from
`terraform.tfstate`, and from every plan output. The `DATABASE_URL` secret is
composed by Terraform from `.address` — never `.endpoint`, which already carries
`:5432` and would silently yield `host:5432:5432`.

**Dump/restore as the experimental control.** See the parity section: holding
database state constant is what makes the two runs comparable at all.

**State honesty.** `terraform.tfstate` is local, gitignored, and contains
`random_password.db` and the composed `DATABASE_URL`. That is accepted for a
one-day, one-operator, torn-down-same-day deployment. The production alternatives
are real and named rather than implied: S3 remote state with SSE and DynamoDB
locking, or `manage_master_user_password = true` to hand the credential to Secrets
Manager and keep it out of state entirely. Pretending the tradeoff was not made
would be worse than the tradeoff.

---

## What broke

Three things, and the honest summary is that this was a quiet deploy. That is not
luck and it is not a claim of skill: the plan's §0.4 front-loaded fourteen verified
facts by executing the repo in a cold sandbox *before* any AWS resource existed, so
most of the discoverable failures were discovered on a laptop where they cost
minutes. The failures below are the ones that survived that filter. Padding this
section would be easy and would make it worthless.

**1. `terraform plan` rejected three security-group descriptions (A2).**

```
Error: "ingress.0.description" doesn't comply with restrictions
("^[0-9A-Za-z_ .:/()#,@\[\]+=&;{}!$*-]*$"):
"API over HTTP from the operator's home IP only"
```

AWS does not allow apostrophes in security-group descriptions. Three of them said
`the operator's home IP`; one also carried an em dash. Prose that reads well in a
code comment is rejected at the API boundary.

The subtlety that made this worth writing down: Terraform stopped at the *first*
offending resource, so only two errors surfaced, while the `svc` and `rds` groups
were never validated and had their own violations queued behind them. Fixing what
the error reported would have produced a second failed plan, then a third. The fix
was instead a small auditor that checks every `description` in the tree against
that charset in one pass — which confirmed `network.tf` clean and correctly ignored
the hits in `variables.tf`/`outputs.tf`, since local Terraform descriptions never
reach the AWS API. The charset is now documented in a comment above the rules.

**2. `/readyz` returned 503 for the first minutes after deploy (A4).**

Immediately after `update-service --force-new-deployment`, `curl` against the API
listener returned 503 while the task was still pulling its image and registering as
a target. It cleared on its own once the target passed its health checks, and smoke
steps 1–5 then went green in order, including `/readyz` over the ALB returning
`{"status":"ok","database":"ok"}` — the SSL, security-group-chain, and restore
proof in a single line.

This was the `health_check_grace_period_seconds = 300` setting doing its job rather
than a fault. Without it the sequence is genuinely destructive: the ALB starts
health-checking a container that is still downloading a ~4 GB image, marks it
unhealthy, ECS kills it, and the replacement restarts the same pull — a crash loop
that presents as an application bug and is purely a timing artifact. Worth knowing
that the 503 window is expected, so nobody spends twenty minutes debugging a
deployment that is merely still starting.

**3. The image was larger than the plan estimated (A1).**

4.06 GB disk usage / 941 MB content size against §A1.2's "expect ~2.5–3.5 GB".
Partly a units artifact — Docker 29 split the old single `SIZE` column into
`DISK USAGE` and `CONTENT SIZE`, and the estimate was written against the old one —
and partly a genuinely larger CPU torch. It was not a gate condition and cost
nothing but push time, but it made §8's "ECR (~3.5 GB)" cost line light, and it is
recorded here rather than quietly corrected.

**Two things that look like failures and are not**, listed because both cost a
double-take: the eval task's **exit code 1** is the gate's verdict surfacing
through `--gate`, exactly as designed; and both ECS services sat **ACTIVE with 0
running** between A2 and A4, which is correct — the ECR repositories were empty
until the images were pushed.

---

## Cost: estimate vs actual

Plan §8 estimated **≈ $2.20** of infrastructure for a ~12-hour day (Fargate $1.23 ·
ALB $0.35 · RDS $0.25 · ECR/S3/Secrets/CloudWatch ~$0.35), plus API spend for the
two eval runs.

Measured API spend is exact and is the larger number: **$7.880494 + $8.007034 =
$15.89** across both runs, comfortably inside the $20 budget on each and needing no
`--yes` override.

The infrastructure actual is computed from lifetime rather than read off a bill,
because the stack existed for **~2.5–3 hours** — `apply` completed around 02:45 PDT
and `destroy` ran between 04:30 and 05:00 PDT on 2026-08-08 — not the ~12 hours the
estimate assumed:

| component | §8 rate | ~2.5–3 h |
|---|---|---:|
| Fargate api (1 vCPU / 8 GB) | $0.0760/hr | $0.19–0.23 |
| Fargate ui (0.25 vCPU / 0.5 GB) | $0.0123/hr | $0.03–0.04 |
| ALB | ~$0.0225/hr + LCU | $0.06–0.07 |
| RDS `db.t4g.micro` + 20 GB gp3 | ~$0.016/hr | $0.04–0.05 |
| **live burn subtotal** | **$0.127/hr** | **$0.32–0.38** |
| Fargate eval task (2 vCPU / 8 GB, 11 m 48 s) | $0.1165/hr | $0.02 |
| ECR storage, 2 secrets, S3, data transfer, LCUs | — | pennies |
| **infrastructure total** | | **≈ $0.40–0.45** |

So: **≈ $0.40–0.45 actual against a $2.20 estimate**, and the honest reading is not
"came in 5× under budget." The per-hour rates in §8 were right — the live burn
really was about $0.127/hr, which is what the table above multiplies. What was
wrong was the *lifetime assumption*: the estimate budgeted a 12-hour day and the
stack lived about three hours. **The pricing model was accurate; the duration model
was 4× conservative.** That is a much less flattering and much more useful thing to
know than a headline underspend.

**Measured API spend is exact and dwarfs the infrastructure: $7.880494 + $8.007034 =
$15.89** across both runs, comfortably inside the $20 budget on each and needing no
`--yes` override. On a day like this the agents cost ~35× the servers.

**On the billing evidence.** Fargate has no free tier, so a small line item *will*
appear — with hours of lag. Day-of Cost Explorer was still blank when
`evidence/billing-day-of.png` was captured, and that screenshot shows a month-to-date
figure of $1.57 which is **not** this deployment's cost: it is account-wide rather
than tag-filtered, and the same breakdown shows unrelated pre-existing S3, Amplify,
Glue, DynamoDB and CloudWatch usage. Reporting "$1.57 actual vs $2.20 estimate"
would have been the easy sentence and a false one. A corroborating screenshot can be
added here once the line items populate; the computed figure above is the claim in
the meantime, and it is stated as a computation, not a measurement.

Leaving the stack up would have cost roughly $3.20/day. Don't; the artifact is the
repo.

---

## Teardown, and three things it taught

`terraform destroy` ran clean in one pass — the V13 flags (`force_delete` on both
ECR repositories, `force_destroy` on the bucket, `recovery_window_in_days = 0` on
both secrets) were set at the start precisely so teardown would not need a manual
console sweep, and they worked. Full output in
[`evidence/teardown-proof.txt`](evidence/teardown-proof.txt).

**1. The tag-scan proof is weaker than the plan assumed.** §A6.3 specifies
`aws resourcegroupstaggingapi get-resources --tag-filters Key=Project,Values=backline`
returning an empty list as the receipt that nothing was orphaned. It does not return
empty. After a successful destroy it still lists six ARNs:

```
arn:aws:ecs:us-west-2:675362625117:service/backline/ui
arn:aws:ecs:us-west-2:675362625117:service/backline/api
arn:aws:ecs:us-west-2:675362625117:task-definition/backline-api:1
arn:aws:ecs:us-west-2:675362625117:task-definition/backline-eval:1
arn:aws:ecs:us-west-2:675362625117:task-definition/backline-ui:1
arn:aws:ecs:us-west-2:675362625117:cluster/backline
```

These are tombstones, not resources. The tagging index retains deleted ECS service
records and deregistered task-definition revisions; a deleted ECS service lingers as
`INACTIVE` rather than disappearing. The authoritative check is therefore
per-service, and every one of them is empty:

| check | result |
|---|---|
| `ecs describe-services --services api ui` | both `INACTIVE`, 0 running |
| `ecs list-clusters` | `[]` |
| `ecs list-task-definitions --status ACTIVE` | `[]` |
| `rds describe-db-instances` | `[]` |
| `elbv2 describe-load-balancers` | `[]` |
| `ec2 describe-vpcs --filters is-default=false` | `[]` |
| `ecr describe-repositories` | `[]` |
| `secretsmanager list-secrets` | `[]` |
| `ec2 describe-security-groups --filters tag:Project=backline` | `[]` |
| `logs describe-log-groups --prefix /ecs/backline` | `[]` |

A one-line tag scan is a nice idea for a teardown receipt and a bad one to trust
unexamined: it answers "what does the tag index remember," not "what still exists."

**2. Resource tags are not cost-filterable unless you activate them first.** The
`Project` and `Ephemeral` tags exist on every resource, but AWS will not filter Cost
Explorer by a tag until it has been activated as a **cost-allocation tag** in
Billing — and activation is not retroactive. It was never activated, so the
`Project = backline` cost view the plan assumed does not exist for this deployment.
Attribution fell back to filtering by service, which is unambiguous *here* only
because the material services — Fargate/ECS, ALB, RDS, ECR, Secrets Manager — had
no prior usage in this account. (S3 and CloudWatch did have prior usage, but the
deploy's share of those is pennies.) On an account with existing ECS or RDS
workloads this fallback would not have worked, and the tags would have been
decorative. Activate cost-allocation tags on day zero, before the resources exist.

**3. `force_destroy` took the evidence bucket with it.** This one is a genuine
own-goal, and it is in the plan's own design: the evidence bucket is a
Terraform-managed resource with `force_destroy = true`, so `terraform destroy`
deleted `backline-evidence-675362625117` along with everything in it — the eval
artifacts *and* the 24 MB migration dump that §A3 archived there as provenance.
`aws s3 ls` now returns `NoSuchBucket`.

Nothing was actually lost, because the artifacts were committed to this repo as
well, which is why `evidence/` exists. But the plan treated S3 as the durable
archive and the repo as a convenience, and that was backwards: the bucket was
inside the blast radius of the very command the plan ends with. An evidence store
that a routine teardown deletes is not an evidence store. Production — or a second
run of this exercise — puts evidence in a bucket outside the Terraform root module,
or gives it `force_destroy = false` and a `prevent_destroy` lifecycle block, and
accepts the manual cleanup as the price of durability.

---

## What production would add

Named, not built, on purpose — each is a real gap and each was a deliberate scope
cut rather than an oversight:

- **Network:** private subnets, VPC endpoints for ECR/Secrets Manager/CloudWatch,
  a NAT for Anthropic egress. Removes public IPs from tasks and public
  accessibility from RDS entirely.
- **TLS and DNS:** an ACM certificate, an HTTPS listener, a real domain. The `/32`
  ingress lock is a substitute for authentication, and it is a substitute that only
  works for one operator on one day.
- **State:** S3 remote state with SSE and DynamoDB locking, or
  `manage_master_user_password = true` so the database credential never enters
  state at all.
- **Delivery:** image builds and pushes driven by CI against a tagged commit, not a
  laptop; task definitions rendered from the same pipeline.
- **Scaling and resilience:** service autoscaling on ALB request count; RDS
  automated backups, Multi-AZ, and a maintenance window.
- **Observability:** CloudWatch alarms and a dashboard rather than `logs tail`, and
  ALB access logs actually queried rather than merely delivered.
- **Cost governance:** `Project`/`Ephemeral` activated as **cost-allocation tags in
  Billing before the first apply** — tags are not filterable in Cost Explorer
  otherwise, and activation is not retroactive (see Teardown lesson 2).
- **Durable evidence:** an artifacts bucket *outside* the Terraform root module, so
  `terraform destroy` cannot delete the record of what was deployed (Teardown
  lesson 3).

---

## Reproduce it

Prerequisites: the A0 checklist (AWS account, Budget at $25, Terraform ≥ 1.9,
Docker with BuildKit, home IP, Anthropic key), and a seeded local world whose
`rag.contract_chunks` carries real bge embeddings.

```bash
# 1. Build and gate the deploy image (offline proof that weights are baked)
docker build -f docker/aws.Dockerfile -t backline-aws:latest .
docker run --rm -e HF_HUB_OFFLINE=1 backline-aws:latest \
  uv run --no-sync python -c "import evals, pathlib; print(len(list(pathlib.Path('/data/inbox').glob('*'))))"

# 2. Stand up the infrastructure
cd deploy/aws && cp terraform.tfvars.example terraform.tfvars   # home_cidr + deployed_git_sha
terraform init && terraform validate && terraform plan
terraform apply                                                  # ~10 min; RDS is the long pole

# 3. Set the key out of band (leading space keeps it out of shell history)
 aws secretsmanager put-secret-value --secret-id backline/anthropic-api-key \
   --secret-string "$ANTHROPIC_API_KEY"

# 4. Migrate the world into RDS
RDS_URL="$(terraform -chdir=deploy/aws output -raw database_url)"
docker compose exec -T db pg_dump -Fc -U backline -d backline > deploy/aws/backline.dump
docker compose exec -T db psql "$RDS_URL" -c "CREATE EXTENSION IF NOT EXISTS vector;"
docker compose exec -T db pg_restore --no-owner --no-privileges --no-comments -d "$RDS_URL" < deploy/aws/backline.dump

# 5. Push images (UI is built here, with the ALB address baked in) and smoke
deploy/aws/scripts/build_push.sh
curl -s "$(terraform -chdir=deploy/aws output -raw api_url)/readyz"   # {"status":"ok","database":"ok"}

# 6. The paired eval
uv run python -m evals run --suite core --model claude-sonnet-5 --budget 20.00   # local control
deploy/aws/scripts/run_eval_task.sh                                              # AWS treatment
deploy/aws/scripts/fetch_summary.sh                                              # artifacts out of RDS

# 7. Tear it down, then verify per-service — NOT with the tag scan, which returns
#    ECS tombstones after a clean destroy (see Teardown lesson 1). Note this also
#    deletes the evidence bucket; commit anything you need to keep first.
terraform -chdir=deploy/aws destroy
aws ecs list-clusters; aws rds describe-db-instances; aws elbv2 describe-load-balancers
aws ec2 describe-vpcs --filters Name=is-default,Values=false
```

`python -m backline.db.migrate` is intentionally absent from step 4 — see
[Migration verification](#migration-verification-a3).
