# BENCHMARK_NOTES — cost / latency / accuracy tradeoffs (Phase 7)

**Status.** The sweep infrastructure shipped with Phase 7
(`benchmarks/run_sweep.py`, `benchmarks/report.py`, `benchmarks/sweep.yaml`,
`benchmarks/LOCAL.md`); the live API rows are the operator's action — this
repo's sessions run keyless (see PHASE_LOG). This document therefore does what
can be done honestly *before* the numbers exist: it fixes the measurement frame,
records the priors, and **pre-registers the hypotheses** the results will
confirm or kill. Sections marked ⏳ are written against
`benchmarks/results/REPORT.md` once `make bench-sweep` (and, later, the
LOCAL.md follow-up) has run. Pre-registration is deliberate: conclusions written
before the data cannot be post-hoc narrative.

---

## 1. What the sweep measures — and what it deliberately holds fixed

One variable moves: **the planner model** driving the agent loops. Everything
else is the shipped platform (D-031):

- same prompts (content-hashed into every trace), same tool set, same retrieval
  stack;
- same per-run limits — 12 iterations, $0.50 per-run budget with the
  Reconciler's empirically-sized $2.50 floor (D-020), 16384-token Reconciler
  output ceiling (D-021);
- same **utility model** (`claude-haiku-4-5`) for compression/summarization on
  every row — model policy is a planner-tier decision (§2); note the haiku row
  is therefore planner == utility;
- same **judge** (`claude-sonnet-5`, rubric-pinned) grading every row, so T3
  scores compare across models instead of drifting with the grader.

The sweep answers the production question — *what happens to the shipped
product when the planner is swapped* — not the isolated-capability question
(*what could each model do with caps tuned to its price*). The costs of that
choice are known and reported rather than hidden: a fixed dollar cap buys an
expensive model fewer tokens (§4, H1), and `runs.exhausted` in every results
document says how often the cap actually bit.

## 2. How to read the numbers

- **overall / category scores** — each question scores the **minimum** of its
  tier scores (T1 exact-match, T2 trace assertions, T3 judge): a right number
  produced by a forbidden process fails, and a beautifully-cited wrong number
  fails. Category = mean × 100.
- **$/query** — the agent loop's metered spend (planner + utility calls, priced
  from `config/models.yaml` via the CostMeter) divided by scored questions.
  **Judge spend is excluded** — you don't judge production traffic — and
  carried separately (`judge_cost_usd`, `usd_per_query_with_judge` for
  reproducing the run's bill).
- **p50/p95 latency** — wall-clock per question including tool execution, at
  the sweep's concurrency (4). Compare shapes, not absolutes.
- **mean iterations** — iteration spans per run; the loop-efficiency signal.
- **tool-error rate** — tool-call spans not ending `ok` (`error` + `timeout` +
  `denied`) over all tool calls; `by_status` in the results JSON splits
  malformed-call errors from guardrail denials.
- **runs.exhausted** — runs ended by the iteration or per-run budget cap;
  the honesty companion to every depressed category score.

Category → agent, for attributing differences: `catalog_lookup` and
`sql_analytics` are Analyst; `contract_terms`, `royalty_math`,
`recoupment_state`, `cross_collateral` are Counsel; `reconciliation` is the
Reconciler; `multi_step` splits 6 Counsel / 6 Reconciler; `abstention` is 8
Counsel / 2 Analyst; `adversarial` spans Counsel and Reconciler.

## 3. Priors — the Phase 5 sonnet arc (suite `6eef41c6706f309a`)

The composite live baseline for `claude-sonnet-5` (D-023) is the one real
datapoint and the sweep's anchor row:

| category | score | note |
|---|---:|---|
| catalog_lookup, royalty_math, recoupment_state, cross_collateral, sql_analytics, abstention | 100.0 | after the D-017..D-022 harness fixes |
| reconciliation | 96.7 | borderline non-flags handled |
| adversarial | 93.3 | injection canary never obeyed |
| contract_terms | 85.0 | T3 prose grades, not retrieval misses |
| multi_step | 72.8 | T1/T2 at 100/98.3 — the 72.8 is judge marks on overreach/hedging prose |

Weighted overall ≈ **94.8**. Run-shape priors from 2b9f39fb: ~$16.74 metered at
sticker 3/15 (≈ $11.16 at the intro 2/10 actually billed), p50 ≈ 15s / p95 ≈
115s at concurrency 4, Reconciler mean ≈ $0.45/question with 6/22 runs
cap-censored at $2.50. The whole diagnosis arc found **zero agent
hallucinations** — every zero traced to the harness — so sweep-row differences
can be read as model differences with unusual confidence.

## 4. Pre-registered hypotheses (2026-08-06, before any sweep row ran)

- **H1 — Opus reconciliation/multi_step scores will be partly cap artifacts.**
  At the Reconciler's token mix (~87K in / ~12.7K out per question), opus bills
  ≈ 1.67× sonnet's sticker metering and 2.5× its intro billing — the fixed
  $2.50 run cap buys opus ≈ 60% of the tokens that already cap-censored 6/22
  sonnet reconciler runs. Prediction: elevated `runs.exhausted` on the opus
  row concentrated in Reconciler categories; read those scores *with* the
  exhaustion count, and expect the honest conclusion to be "opus needs a
  cap-normalized re-run (`RUN_BUDGET_USD` up, budget envelope re-authorized)
  before its workflow scores mean anything."
- **H2 — Haiku loses on iteration discipline before it loses on knowledge.**
  Cheap-model failure mode in this repo's logs is repeated failed `sql_query`
  attempts and schema probing. Prediction: haiku's `iterations_mean` and
  tool-error rate are the highest of the API rows; its exhaustions are
  iteration-capped (not budget-capped — at 1/5 prices the $2.50 floor buys ~3×
  sonnet-sticker tokens); Analyst categories (`catalog_lookup`,
  `sql_analytics`) hold up better than Counsel money categories.
- **H3 — The frontier is concave with sonnet on the knee.** Opus's ≈ 2.4×
  $/query over sonnet (5/25 vs 2/10) buys, at most, prose-quality points on
  `contract_terms`/`multi_step` T3 — the T1/T2 layers are already ~saturated
  by sonnet (§3), leaving opus ≤ ~5 overall points of headroom on this suite.
  Prediction: $/overall-point is strictly worse for opus; the sonnet row
  dominates the accuracy-per-dollar frontier except possibly at the
  judge-graded categories.
- **H4 — Abstention and adversarial are where the cheap row is riskiest.**
  Typed abstention and injection non-compliance are instruction-following
  under pressure. Prediction: if haiku drops whole categories, these are the
  first two; a haiku `adversarial` < 100 with a `guardrail` flag still raised
  would be the platform working as designed (defense-in-depth catching what
  the model missed) and worth a paragraph in the README.
- **H5 — Tool-error rate predicts score better than price does.** Across the
  three API rows, category score should track (negatively) with tool-error
  rate and iteration inflation more tightly than with $/Mtok — the platform
  thesis in one measurable: reliability of *process*, not raw model size, is
  what the money categories pay for.

## 5. ⏳ The accuracy/$ frontier

*Fill from `benchmarks/results/REPORT.md` after `make bench-sweep`: the
headline table, `comparison.svg`, and per-category deltas. Verdicts on H1
(with `runs.exhausted` split), H3, H5. Where each marginal dollar goes.*

## 6. ⏳ Where the cheap model is good enough

*Per-category verdict for `claude-haiku-4-5` — the "route this to the cheap
tier" list for the Router's model policy, with the H2/H4 outcomes. Expected
shape: Analyst lookups route cheap; money math and workflow stay on the knee.*

## 7. ⏳ Tool-calling reliability differences

*`tool_calls.by_status` across rows: malformed-argument errors vs guardrail
denials vs timeouts; retry-loop shapes from the traces; whether D-021's
truncation contract fired on any row. The raw material for the README's
tool-use reliability paragraph.*

## 8. ⏳ The local row (`local-qwen`, per `benchmarks/LOCAL.md`)

*Follow-up run, operator-executed on the rig. Expected read: tool-call format
fidelity (the qwen3 parser, not `hermes`) and instruction-following under the
8K context are the gating factors, with $/query an honest zero — the row exists
to measure where "free" sits on the frontier, not to win it.*

## 9. Known limits of the method

- One run per row — no variance bars; treat ≤ ~3-point category deltas as
  noise unless the trace says otherwise (suite n per category is 3–25).
- Fixed caps favor cheap models on workflow categories (H1) — deliberate, and
  visible via `runs.exhausted` rather than corrected-for.
- The judge is sonnet grading sonnet on one row — a family-bias risk accepted
  for judge comparability, mitigated by T3 being floored under T1/T2 minima
  and the rubric being citation-anchored (it grades faithfulness to quoted
  clauses, not style affinity).
- Latency is measured through one household's network at concurrency 4 —
  shape-comparable across rows, not an SLA claim.
