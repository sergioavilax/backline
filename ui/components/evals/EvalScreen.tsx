"use client";

/** Eval Dashboard (§6 surface 4): run list → category matrix vs baseline →
 *  drill-down to a failed question with expected vs actual + link to its trace. */

import { useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { apiGet } from "@/lib/api";
import { ago, money, timestamp } from "@/lib/format";
import type { Baseline, EvalResult, EvalRun, EvalRunDetail } from "@/lib/types";
import { JsonTree } from "@/components/JsonTree";
import { EmptyState, PanelHeader, SkeletonRows } from "@/components/ui";

interface CategoryRow {
  category: string;
  n: number;
  score: number;
  tiers: Record<string, number>;
  baseline: number | null;
}

function scoreColor(score: number): string {
  if (score >= 95) return "text-green";
  if (score >= 80) return "text-text";
  return "text-red";
}

function DeltaChip({ delta }: { delta: number | null }) {
  if (delta === null) return <span className="text-faint">—</span>;
  const rounded = Math.round(delta * 10) / 10;
  if (Math.abs(rounded) < 0.05) return <span className="text-faint">±0.0</span>;
  // The CI gate fails a category dropping more than 3 points (§5.4).
  const cls = rounded > 0 ? "text-green" : rounded <= -3 ? "text-red" : "text-amber";
  return (
    <span className={`mono ${cls}`}>
      {rounded > 0 ? "+" : ""}
      {rounded.toFixed(1)}
    </span>
  );
}

function QuestionDrill({ results }: { results: EvalResult[] }) {
  const [failuresOnly, setFailuresOnly] = useState(true);
  const [open, setOpen] = useState<string | null>(null);

  const byQuestion = useMemo(() => {
    const grouped = new Map<string, EvalResult[]>();
    for (const result of results) {
      grouped.set(result.question_id, [...(grouped.get(result.question_id) ?? []), result]);
    }
    return [...grouped.entries()].map(([id, tiers]) => ({
      id,
      tiers,
      category: tiers[0]?.detail.category ?? "?",
      runId: (tiers[0]?.detail.run_id as string | null) ?? null,
      failed: tiers.some((tier) => tier.passed === false),
    }));
  }, [results]);

  const shown = failuresOnly ? byQuestion.filter((q) => q.failed) : byQuestion;

  return (
    <div className="p-4">
      <div className="mb-2 flex items-center gap-3">
        <span className="label">
          questions <span className="mono">({shown.length})</span>
        </span>
        <label className="flex cursor-pointer items-center gap-1.5 text-[11px] text-dim">
          <input
            type="checkbox"
            checked={failuresOnly}
            onChange={(event) => setFailuresOnly(event.target.checked)}
            className="accent-[#f2a33c]"
          />
          failures only
        </label>
      </div>
      {shown.length === 0 ? (
        <div className="text-[12px] text-faint">
          {failuresOnly ? "No failing questions in this run." : "No results recorded."}
        </div>
      ) : (
        <div className="flex flex-col gap-1">
          {shown.map((question) => (
            <div key={question.id} className="rounded-[6px] border border-edge bg-panel">
              <button
                type="button"
                onClick={() => setOpen(open === question.id ? null : question.id)}
                className="flex w-full items-center gap-2 px-3 py-2 text-left hover:bg-panel-2/60"
              >
                <span className="mono text-[12px]">{question.id}</span>
                <span className="label">{String(question.category)}</span>
                <span className="ml-auto flex gap-1.5">
                  {question.tiers.map((tier) => (
                    <span
                      key={tier.tier}
                      className={`mono rounded-[3px] border px-1 text-[10px] ${
                        tier.passed === false
                          ? "border-red/40 text-red"
                          : "border-edge text-dim"
                      }`}
                    >
                      {tier.tier} {tier.score !== null ? Number(tier.score).toFixed(2) : "—"}
                    </span>
                  ))}
                </span>
              </button>
              {open === question.id && (
                <div className="border-t border-edge px-3 py-2">
                  {question.runId && (
                    <div className="mb-2">
                      <Link
                        href={`/runs/${question.runId}`}
                        className="text-[11px] text-blue hover:underline"
                      >
                        view full trace →
                      </Link>
                    </div>
                  )}
                  {question.tiers.map((tier) => (
                    <div key={tier.tier} className="mb-2">
                      <div className="label mb-1">{tier.tier} detail</div>
                      <JsonTree data={tier.detail} />
                    </div>
                  ))}
                </div>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

export function EvalScreen() {
  const [runs, setRuns] = useState<EvalRun[] | null>(null);
  const [baseline, setBaseline] = useState<Baseline | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<EvalRunDetail | null>(null);

  useEffect(() => {
    apiGet<{ runs: EvalRun[] }>("/evals/runs")
      .then((data) => {
        setRuns(data.runs);
        if (data.runs.length > 0) setSelectedId((id) => id ?? data.runs[0].id);
      })
      .catch(() => setRuns([]));
    apiGet<Baseline>("/evals/baseline")
      .then(setBaseline)
      .catch(() => setBaseline(null));
  }, []);

  useEffect(() => {
    setDetail(null);
    if (selectedId === null) return;
    apiGet<EvalRunDetail>(`/evals/runs/${selectedId}`)
      .then(setDetail)
      .catch(() => setDetail(null));
  }, [selectedId]);

  const selected = runs?.find((run) => run.id === selectedId) ?? null;

  const matrix: CategoryRow[] = useMemo(() => {
    if (selected?.summary.categories === undefined) return [];
    const track = selected.summary.track ?? "platform";
    const subset = selected.summary.subset ?? "full";
    const base = baseline?.baselines.find(
      (entry) =>
        entry.model === selected.model && entry.track === track && entry.subset === subset,
    );
    return Object.entries(selected.summary.categories).map(([category, bucket]) => ({
      category,
      n: (bucket as { n?: number }).n ?? 0,
      score: (bucket as { score?: number }).score ?? 0,
      tiers: ((bucket as { tiers?: Record<string, number> }).tiers ?? {}) as Record<
        string,
        number
      >,
      baseline: base?.categories[category] ?? null,
    }));
  }, [selected, baseline]);

  return (
    <div className="flex h-screen">
      <aside className="flex w-[300px] shrink-0 flex-col border-r border-edge bg-panel">
        <PanelHeader>
          <span className="label">Eval Runs</span>
        </PanelHeader>
        <div className="min-h-0 flex-1 overflow-y-auto" data-testid="eval-run-list">
          {runs === null && <SkeletonRows rows={4} />}
          {runs !== null && runs.length === 0 && (
            <EmptyState>
              No eval runs in this database yet — run `make eval-smoke` or the live suite and
              they land here.
            </EmptyState>
          )}
          {runs?.map((run) => (
            <button
              key={run.id}
              type="button"
              onClick={() => setSelectedId(run.id)}
              className={`block w-full border-b border-edge/60 px-3 py-2 text-left hover:bg-panel-2/60 ${
                run.id === selectedId ? "bg-panel-2" : ""
              }`}
            >
              <div className="flex items-center gap-2 text-[12px]">
                <span className="truncate">{run.model}</span>
                <span className="label ml-auto">
                  {String(run.summary.track ?? "?")}
                  {run.summary.subset ? ` · ${run.summary.subset}` : ""}
                </span>
              </div>
              <div className="mt-0.5 flex justify-between text-[10px] text-faint">
                <span className="mono">
                  {String(run.summary.n_scored ?? "…")}/{String(run.summary.n_questions ?? "?")} q
                </span>
                <span>{ago(run.started_at)}</span>
              </div>
            </button>
          ))}
        </div>
      </aside>

      <section className="flex min-w-0 flex-1 flex-col overflow-y-auto">
        {selected === null ? (
          <EmptyState>Select an eval run.</EmptyState>
        ) : (
          <>
            <div className="border-b border-edge px-4 py-3">
              <div className="flex items-center gap-3">
                <h1 className="text-[20px] font-medium">{selected.model}</h1>
                <span className="label">
                  {String(selected.summary.track ?? "")} ·{" "}
                  {String(selected.summary.subset ?? "full")}
                </span>
                {typeof selected.summary.t2_violations === "number" &&
                  selected.summary.t2_violations > 0 && (
                    <span className="label border border-red/40 px-1 py-px text-red">
                      {selected.summary.t2_violations} T2 violations
                    </span>
                  )}
              </div>
              <div className="mt-1.5 flex flex-wrap gap-x-4 gap-y-1 text-[11px] text-dim">
                <span>
                  suite <span className="mono">{selected.suite_hash}</span>
                </span>
                {selected.git_sha && (
                  <span>
                    git <span className="mono">{selected.git_sha.slice(0, 12)}</span>
                  </span>
                )}
                <span>
                  started <span className="mono">{timestamp(selected.started_at)}</span>
                </span>
                {typeof selected.summary.total_cost_usd === "string" && (
                  <span>
                    spend{" "}
                    <span className="mono">${money(selected.summary.total_cost_usd)}</span>
                  </span>
                )}
              </div>
            </div>

            <div className="p-4">
              <div className="label mb-2">category × score (vs committed baseline)</div>
              {matrix.length === 0 ? (
                <div className="text-[12px] text-faint">This run has no category summary.</div>
              ) : (
                <table className="w-full max-w-2xl text-[12px]" data-testid="category-matrix">
                  <thead>
                    <tr className="label text-left">
                      <th className="border-b border-edge py-1.5 pr-2 font-normal">category</th>
                      <th className="border-b border-edge py-1.5 pr-2 text-right font-normal">
                        n
                      </th>
                      <th className="border-b border-edge py-1.5 pr-2 text-right font-normal">
                        score
                      </th>
                      <th className="border-b border-edge py-1.5 pr-2 text-right font-normal">
                        Δ base
                      </th>
                      <th className="border-b border-edge py-1.5 pr-2 text-right font-normal">
                        t1
                      </th>
                      <th className="border-b border-edge py-1.5 pr-2 text-right font-normal">
                        t2
                      </th>
                      <th className="border-b border-edge py-1.5 text-right font-normal">t3</th>
                    </tr>
                  </thead>
                  <tbody>
                    {matrix.map((row) => (
                      <tr key={row.category} className="border-b border-edge/60">
                        <td className="py-1.5 pr-2">{row.category}</td>
                        <td className="mono py-1.5 pr-2 text-right text-dim">{row.n}</td>
                        <td
                          className={`mono py-1.5 pr-2 text-right ${scoreColor(row.score)}`}
                        >
                          {row.score.toFixed(1)}
                        </td>
                        <td className="py-1.5 pr-2 text-right">
                          <DeltaChip
                            delta={row.baseline === null ? null : row.score - row.baseline}
                          />
                        </td>
                        {["t1", "t2", "t3"].map((tier) => (
                          <td
                            key={tier}
                            className="mono py-1.5 pr-2 text-right text-dim last:pr-0"
                          >
                            {row.tiers[tier] !== undefined ? row.tiers[tier].toFixed(1) : "—"}
                          </td>
                        ))}
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>

            {detail !== null && <QuestionDrill results={detail.results} />}
          </>
        )}
      </section>
    </div>
  );
}
