# Architecture

Backline is a *platform with three agents on it*, not a single agent: the
platform primitives — loop, tools, memory, routing, guardrails, tracing,
evals — are the deliverable, and the agents prove the primitives are reusable.
The production environment is deliberately a reviewer's laptop:
`docker compose up` must yield a seeded label, three working agents, a live
trace panel, and an eval dashboard. One datastore (Postgres 16 + pgvector)
carries relational facts, vectors, FTS, staging queues, traces, and eval
results ([D-000](DECISIONS.md)).

```
                                ┌────────────────────────────────────────────┐
                                │                  ui/  (Next.js)            │
                                │  Chat · Trace Inspector · Review Queue ·   │
                                │  Eval Dashboard                            │
                                └──────────────┬─────────────────────────────┘
                                               │ REST + SSE
┌───────────────┐               ┌──────────────┴─────────────────────────────┐
│  datagen/     │  seed/emit    │           backline/api  (FastAPI)          │
│  world +      ├──────────────►│  /sessions /runs /review /evals /catalog   │
│  answer key   │               └──────────────┬─────────────────────────────┘
└──────┬────────┘                              │
       │ writes                 ┌──────────────┴─────────────────────────────┐
       ▼                        │              backline/core                 │
┌────────────────┐              │  Router → AgentRuntime(loop) → Tools       │
│ Postgres 16    │◄────────────►│  Memory · Guardrails · Tracer · CostMeter  │
│ + pgvector     │              └───────┬──────────────┬─────────────────────┘
│  label / app / │                      │              │
│  staging/truth │              ┌───────┴──────┐ ┌─────┴───────────────┐
│  / rag         │              │ providers/   │ │ tools/              │
└────────────────┘              │ anthropic    │ │ sql · retrieve ·    │
        ▲                       │ openai_compat│ │ calc · statements · │
        │ reads + gates         │ mock         │ │ scan · notes        │
   evals/ · benchmarks/         └──────────────┘ └─────────────────────┘
```

## Providers — the only gate for LLM calls

No LLM call exists outside a `Provider` implementation (invariant 6). All
three normalize to one internal wire shape (`Message` / `ToolCall` /
`CompletionResult`, five stop reasons) so the runtime never sees vendor JSON
([D-007](DECISIONS.md)):

- **AnthropicProvider** — official SDK, always streaming, accumulating partial
  `input_json_delta` tool arguments; SDK-delegated retries with jittered
  backoff. Unit-tested offline against canned SSE via `httpx.MockTransport`.
- **OpenAICompatProvider** — raw httpx against any OpenAI-format endpoint
  (vLLM for local models); JSON-string tool arguments parsed here, unparseable
  ones surfaced loudly as provider errors.
- **MockProvider** — an ordered script of turns with per-turn request
  matchers. Every unit/integration test and the keyless demo mode run on it;
  zero tests require an API key (invariant 8).

`config/models.yaml` maps model id → provider, context window, and USD prices
per Mtok as quoted strings parsed to `Decimal` (a bare YAML float refuses to
load — money is never float). A model may carry a *dated* price schedule; the
registry resolves the tier for the load date and records the choice, so the
cost meter matches the invoice across price transitions ([D-017](DECISIONS.md)).

## The agent runtime

One loop (`backline/core/runtime.py`) runs every agent:

1. Assemble context: system prompt + session window + working set + last tool
   results.
2. `provider.complete(tools=...)`.
3. Tool calls: Pydantic arg validation → guardrail checks → execute (with
   timeout) → results appended (oversize results summarized by the utility
   model as a traced `compression` span).
4. Text turn without tool calls: finalize into a typed `FinalAnswer`
   (Counsel/Analyst: `answer`, `citations[]`, `abstained`; Reconciler adds
   `batch_id`, `flags_summary` parsed from `BATCH:`/`FLAGS:` wrap-up lines —
   [D-014](DECISIONS.md)).
5. Every step is a span; every call is metered.

Semantics worth naming ([D-009](DECISIONS.md), [D-021](DECISIONS.md)):

- **Limits end runs loudly.** Budget/iteration exhaustion → `status=exhausted`
  with a run-level guardrail span, never a silent truncation. Tool failures
  (bad args, unknown tool, timeout, handler exception) return to the *model*
  as error results — it gets a chance to correct itself; only provider errors
  end the run.
- **A `max_tokens`-truncated reply is never acted on.** Cut tool calls are
  discarded un-executed (a streamed-prefix dict can *validate* and still not
  be what the model said); cut text never finalizes. Both paths record an
  `output_truncated` guardrail incident and ask the model to re-issue.
- **Per-agent limits are empirical, not guessed.** Defaults: 12 iterations,
  $0.50/run, 30s/tool, ~2K-token results, 4096 output tokens. The Reconciler
  — a workflow, not a Q&A turn — gets 24 iterations, a $2.50 budget floor
  sized from measured workflow cost ([D-020](DECISIONS.md)), 120s tool
  timeout, 4K-token results, and a 16384 output ceiling sized from the
  measured worst-case `submit_batch` payload ([D-021](DECISIONS.md)).

## Tools

Pydantic-typed, self-describing, registered per agent (BUILD_PLAN §4.3 plus
the two Phase 4 additions, [D-013](DECISIONS.md)):

| Tool | Agents | Contract |
|---|---|---|
| `search_contracts` | Counsel, Reconciler | governing-filtered hybrid retrieval; opens with the artist's governing-document inventory ([D-028](DECISIONS.md)); structural citations `{contract_id, clause_no}` |
| `read_clause` | Counsel | exact clause fetch; misses list what exists; supersession noted |
| `calc_royalties` | Counsel, Reconciler | the one royalty engine over DB-assembled inputs ([D-012](DECISIONS.md)); ledger + spot modes; all money arithmetic goes here |
| `sql_query` | Analyst, Reconciler | parser-level read-only policy (below); auto-LIMIT; EXPLAIN cost ceiling |
| `ingest_statement` | Reconciler | parse an inbox CSV through the 6-dialect normalizer into `staging.ingested_lines` only ([D-010](DECISIONS.md)) |
| `match_lines` | Reconciler | ISRC/UPC → catalog partition (matched/unmatched) |
| `scan_anomalies` | Reconciler | deterministic tolerance rule per anomaly kind; within-tolerance measurements reported, not flagged ([D-013](DECISIONS.md)) |
| `compute_allocations` | Reconciler | whole-period allocations through the one engine, materiality floor, bounded concurrency |
| `submit_batch` | Reconciler | **the only write path an agent has** — a `proposed` batch in `staging`, run-stamped |
| `save_note` / `recall_notes` | all | durable entity-keyed observations (`app.notes`) |

## Guardrails — layered, and visible

- **SQL policy at the parser** (`tools/sqlpolicy.py`): sqlglot parse → exactly
  one SELECT, schema allowlist `{label, staging}` — `truth` (the answer key),
  `app`, `rag`, and `pg_catalog` are dead by construction, with a canary test
  pinning the exclusion; no DML/DDL, side-effect functions denylisted, LIMIT
  injected/capped, EXPLAIN cost ceiling before execution.
- **Write gating by schema**: no tool can touch `label.*`; the Reconciler's
  proposals land in `staging` and promotion is a human review action.
- **Run caps**: per-run budget and iteration limits (above), suite-level
  budgets in the eval/benchmark runners with committed-spend accounting
  ([D-019](DECISIONS.md)).
- **Injection defense** ([D-013](DECISIONS.md)): retrieved corpus text is
  fenced in `<document>` tags (data, not instructions); a post-execution
  `ResultCheck` regex family flags suspicious document content
  (`injection_suspected` guardrail span + a one-line notice the model sees)
  without blocking — the model must *see and refuse*, and the eval asserts
  both the flag and the refusal. A corpus-wide sweep test pins exactly one
  trip: the seeded canary (FBR-C-00670 §7).
- Every denial/flag is a `guardrail` span rendered in the Trace Inspector —
  incidents are UI objects, not log lines.

## Memory

Three scopes, deliberately boring: session memory (SQL-windowed last 20 turns
with a deterministic elision note), working memory (per-run tool-result dedup
by `(tool, content)` hash), and long-term notes (`save_note`/`recall_notes`,
auto-recalled into the user turn — visibly fenced — when the router detects an
artist match).

## Agents & router

Three configurations of the one runtime (`backline/agents/`): prompts are
versioned files whose sha256 rides in every run's trace attrs, so eval results
pin to exact prompt versions. The router is a Haiku-class front door making
one forced `route` tool call in its own traced run; below the confidence
threshold it downgrades to `clarify` (never guesses, never crashes on model
judgment). Model policy is three settings — `PLANNER_MODEL`, `UTILITY_MODEL`,
`ROUTER_MODEL` — so routing exists at two levels: agent selection and model
selection ([D-014](DECISIONS.md)).

## RAG — structured first

```
query (artist, as_of_date)
  │
  ▼
governing-document filter (SQL) ── bases effective by date + amendments,
  │                                minus superseded sections, at clause
  │                                granularity (D-002, D-003)
  ▼
hybrid retrieval over governing chunks only
  ├─ Postgres FTS (ts_rank_cd, weighted tsvector)
  └─ pgvector cosine (bge-small-en-v1.5, 384-dim, CPU)
  ▼
Reciprocal Rank Fusion (k=60, 50/leg)
  ▼
cross-encoder rerank (ms-marco-MiniLM-L-6-v2, fused top-30 only; RERANK=off to compare)
  ▼
results: governing inventory + query-aware snippets + structural citations
```

Chunks *are* clauses — the PDF renderer emits deterministic numbered headings,
and chunking parses its `.txt` sidecars, so citations are structural
(`FBR-C-00501 §3`), not vibes. Supersession is applied *before* ranking: a
replaced rate clause is unfindable, not merely outranked. The measured
consequence: governing-scoped MRR 0.398 / R@10 0.97 vs unscoped MRR 0.000 on
the same queries ([D-002](DECISIONS.md), PHASE_LOG Phase 3). Tool output
renders coverage, not just ranking — every artist-scoped search opens with the
complete governing-document inventory, and `read_clause` marks superseded text
([D-028](DECISIONS.md)).

The embedding stack has deterministic offline twins (feature-hashed BoW
embedder, lexical reranker) so keyless CI and model-less machines run the same
mechanism honestly; the chunk store records which model built it and refuses
mismatched queries ([D-011](DECISIONS.md)). `make embed` reconciles by content
hash — unchanged chunks keep their embeddings — and builds the ivfflat index
after bulk insert.

## Data model

Five Postgres schemas, one trust boundary each:

- **`label`** — the operational facts: artists, releases, tracks, contracts +
  canonical JSON terms + amendments, advances/expenses, recoup accounts,
  distributors, statements, `statement_lines` (468K rows, NUMERIC(18,6)),
  monthly FX, dashboard reference streams. Read-only to agents via `sql_query`.
- **`staging`** — the propose side of HITL: `statement_batches`,
  `proposed_allocations`, `flags`, `ingested_lines`. The only agent-writable
  schema, through `submit_batch`/`ingest_statement` only.
- **`truth`** — the answer key: `expected_ledger`, `anomaly_registry`,
  `qa_answer_key`. Structurally unreachable by agents (invariant 3).
- **`app`** — platform state: sessions, messages, runs, spans, notes,
  eval_runs, eval_results.
- **`rag`** — `contract_chunks` (clause chunks, weighted tsvector, 384-dim
  vectors, per-row embedding model + content hash). Reachable only through
  the retrieval tools, not `sql_query`.

Migrations are raw SQL applied in filename order by `backline.db.migrate`
([D-000](DECISIONS.md)); applied migrations are never edited.

## Human-in-the-loop

`ingest_statement` stages lines; statements stay `received`. `submit_batch`
lands a `proposed` batch with allocations and flags. The Review Queue serves
everything the decision needs (per-artist ledger detail, flags with resolved
evidence lines, a "what changes if approved" preview) and the transitions are
guarded SQL: approve/reject only from `proposed`, concurrent reviewers get a
409, reject requires a note. **Approval promotes**: staged lines copy into
`label.statement_lines`, statements flip to `ingested`, the batch is the audit
record ([D-025](DECISIONS.md)). No agent-reachable path can do any of that —
asserted by test.

## Tracing & cost

Span tree per run — `run → iteration → {llm_call | tool_call | guardrail |
compression}` — with OTel-shaped `gen_ai.*` attrs (tokens, cost, latency,
model, tool, status). Sinks: Postgres (insert-on-start / complete-on-end, so
in-flight spans are queryable live — [D-008](DECISIONS.md)), JSONL per run,
and an in-process pubsub feeding the SSE stream. Cost is Decimal end-to-end
through the same rounding policy as royalties (`money6` — API spend is money
too), priced from the registry per call.

## API & UI

FastAPI (22 paths, committed OpenAPI schema drift-tested in CI): sessions +
SSE chat (`accepted → routed → run_started → final`, turns running in
background tasks so a dropped client never kills a run), span snapshot +
live stream (in-proc pubsub merged with a Postgres poll for runs driven by
other processes), review list/detail/approve/reject, evals browse, catalog
browse with clause resolution for citation chips ([D-026](DECISIONS.md)).
With no provider configured the API serves **demo mode**: a deterministic
MockProvider script per message driven through the production stack — real
router, tools, SQL, staging writes, tracing — labeled `demo: true`
([D-024](DECISIONS.md)).

The UI (Next.js 15, tokens in [UI_DIRECTION.md](UI_DIRECTION.md)) ships four
surfaces: Chat (routing badge, clause-chip citations opening a source drawer,
abstention as a quiet state), the live Trace Inspector (the signature: a span
tree filling in real time, amber pulse on the active span, cost ticking in
mono), the keyboard-first Review Queue (j/k/a/r, reject-requires-note), and
the Eval Dashboard (category × model matrix, Δ-vs-baseline chips at the gate's
−3pt threshold, drill-down to a failed question's trace). Agent-authored JSONB
renders verbatim with robust formatters — reviewers judge what the agent
actually wrote ([D-027](DECISIONS.md)).

## Evals — the centerpiece

The domain gives what most LLM evals lack: *exact ground truth*. The suite
(133 questions, 10 categories, committed as a content-hashed golden artifact)
is generated offline from the same in-memory world the DB is seeded from;
hand-authored hard cases carry prompts whose expectations are resolver-derived
from the answer key, so no committed number can drift ([D-015](DECISIONS.md)).

Three tiers, and **a question scores the minimum of its tiers**:

- **T1 exact-match** — tolerance money, exact counts, order-free sets, typed
  abstention; reconciliation scored as flag precision/recall/F1 against the
  registry, borderline non-flags included.
- **T2 trace assertions** — mechanical span-tree checks: calculator used for
  money, governing clause cited, SQL clean, `truth` never touched, exactly
  one/no batch, injection flagged, canary not obeyed. A guardrail *denial* is
  a violation — the attempt shows intent.
- **T3 LLM-judge** — pinned content-hashed rubric, judge model + rubric hash
  recorded per result; the judge sees cited clause texts, never the expected
  answer.

Around the scorers: an async budget-guarded resumable runner (projection →
refuse without `--yes` → committed-spend hard stop mid-run), B0
(context-stuffing) and B1 (naive vector RAG) baseline tracks, a keyed
baseline + regression gate (fail on >3-point category drop, any T2 violation,
stale suite hash, or partial run — [D-016](DECISIONS.md)), a composite
protocol for multi-run baselines under refusal rules ([D-023](DECISIONS.md)),
and infra-error quarantine with in-place healing so provider outages can never
masquerade as model incapability ([D-032](DECISIONS.md)). `make eval-smoke`
runs the entire harness keylessly on every PR and gates against committed mock
entries — the gate mechanism itself executes with teeth before any key exists.

## Benchmarks

`benchmarks/run_sweep.py` swaps exactly one variable — the planner model —
and holds the shipped platform fixed (prompts, tools, caps, judge), answering
the production question rather than the isolated-capability one
([D-031](DECISIONS.md)). Two-level resume (sweep state + runner
skip-scored-questions) makes long unattended runs crash-safe; per-model
results documents carry accuracy by category, agent-only $/query, latency
percentiles, iteration/tool-error/exhaustion counts, token totals, and price
provenance. `benchmarks/report.py` renders the table + frontier chart and
degrades gracefully to API-only rows, naming what is pending. Analysis with
pre-registered hypotheses and adjudicated verdicts:
[BENCHMARK_NOTES.md](BENCHMARK_NOTES.md).

## Determinism & reproducibility

Everything derives from `WORLD_SEED` through named RNG streams with one
construction site (grep-enforced). Three committed fingerprints pin the
artifacts: the world content hash (17 table hashes + 842 file hashes —
[D-006](DECISIONS.md)), the eval suite hash (byte-exact regeneration checked
in CI), and prompt/rubric sha256s recorded in every run and eval result.
Changing generation deliberately means regenerating the golden in the same PR
and saying so; an unexplained diff means the answer key moved. Eval results
key to `(suite_hash, model, git_sha)`, which is what makes composite
baselines and cross-run comparisons legitimate at all.
