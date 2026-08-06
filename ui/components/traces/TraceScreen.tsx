"use client";

/** Trace Inspector (§6 surface 2): run list → live span tree → span detail.
 *
 *  The span tree streams over SSE while a run is live (amber pulse on active
 *  spans, cost ticking in the header); finished runs replay their persisted tree.
 *  Guardrail incidents are highlighted; every span opens a detail drawer with its
 *  full attrs (tool args, token counts, latency) as a JSON tree. */

import { useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import { apiGet } from "@/lib/api";
import { ago, durationMs, shortId, timestamp } from "@/lib/format";
import type { Run, RunList, Span } from "@/lib/types";
import { useSpanStream } from "@/lib/useSpanStream";
import { JsonTree } from "@/components/JsonTree";
import { SpanTree } from "@/components/SpanTree";
import {
  AgentBadge,
  Cost,
  EmptyState,
  PanelHeader,
  SkeletonRows,
  StatusPill,
} from "@/components/ui";

const AGENT_FILTERS = ["all", "counsel", "analyst", "reconciler", "router"] as const;

function RunHeader({ run, live, spans }: { run: Run; live: boolean; spans: Span[] }) {
  const llmCalls = spans.filter((span) => span.kind === "llm_call").length;
  const toolCalls = spans.filter((span) => span.kind === "tool_call").length;
  const guardrails = spans.filter((span) => span.kind === "guardrail").length;
  const models = [
    ...new Set(
      spans
        .map((span) => span.attrs["gen_ai.request.model"])
        .filter((model): model is string => typeof model === "string"),
    ),
  ];
  return (
    <div className="border-b border-edge px-4 py-3" data-testid="run-header">
      <div className="flex items-center gap-2">
        <AgentBadge agent={run.agent} />
        <StatusPill status={live ? "running" : run.status} />
        <span className="mono text-[11px] text-faint">{run.id}</span>
        <span className={`mono ml-auto text-[15px] ${live ? "text-amber" : "text-text"}`}>
          <Cost value={run.cost_usd} className="text-[15px]" />
        </span>
      </div>
      <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-[11px] text-dim">
        <span>
          started <span className="mono">{timestamp(run.started_at)}</span>
        </span>
        <span>
          duration{" "}
          <span className="mono">{durationMs(run.started_at, run.finished_at)}</span>
        </span>
        <span>
          <span className="mono">{llmCalls}</span> llm · <span className="mono">{toolCalls}</span>{" "}
          tool
          {guardrails > 0 && (
            <>
              {" "}
              · <span className="mono text-red">{guardrails}</span> guardrail
            </>
          )}
        </span>
        {models.length > 0 && <span className="mono">{models.join(", ")}</span>}
        {typeof run.meta["prompt_sha256"] === "string" && (
          <span>
            prompt <span className="mono">{String(run.meta["prompt_sha256"])}</span>
          </span>
        )}
      </div>
    </div>
  );
}

function SpanDetail({ span, onClose }: { span: Span; onClose: () => void }) {
  return (
    <aside className="drawer-in flex w-[380px] shrink-0 flex-col border-l border-edge bg-panel-2">
      <PanelHeader>
        <span className="text-[12px]">{span.name}</span>
        <span className="label">{span.kind}</span>
        <button
          type="button"
          onClick={onClose}
          className="ml-auto rounded px-1.5 text-dim hover:text-text"
          aria-label="close span detail"
        >
          ✕
        </button>
      </PanelHeader>
      <div className="min-h-0 flex-1 overflow-y-auto p-4">
        <div className="mb-3 flex flex-col gap-1 text-[11px] text-dim">
          <span>
            started <span className="mono">{timestamp(span.started_at)}</span>
          </span>
          <span>
            duration <span className="mono">{durationMs(span.started_at, span.ended_at)}</span>
          </span>
          <span>
            span <span className="mono">{span.id}</span>
          </span>
        </div>
        <div className="label mb-1">attrs</div>
        <JsonTree data={span.attrs} />
      </div>
    </aside>
  );
}

export function TraceScreen({ runId }: { runId: string | null }) {
  const router = useRouter();
  const [runs, setRuns] = useState<Run[] | null>(null);
  const [agentFilter, setAgentFilter] = useState<(typeof AGENT_FILTERS)[number]>("all");
  const [selectedSpan, setSelectedSpan] = useState<Span | null>(null);
  const { run, spans, live } = useSpanStream(runId);

  useEffect(() => {
    const query = agentFilter === "all" ? "" : `&agent=${agentFilter}`;
    apiGet<RunList>(`/runs?limit=80${query}`)
      .then((list) => setRuns(list.runs))
      .catch(() => setRuns([]));
  }, [agentFilter, runId]);

  // Keep the selected span fresh as its end event streams in.
  const selected = useMemo(
    () => (selectedSpan ? (spans.find((span) => span.id === selectedSpan.id) ?? null) : null),
    [selectedSpan, spans],
  );

  return (
    <div className="flex h-screen">
      <aside className="flex w-[300px] shrink-0 flex-col border-r border-edge bg-panel">
        <PanelHeader>
          <span className="label">Runs</span>
          <select
            value={agentFilter}
            onChange={(event) =>
              setAgentFilter(event.target.value as (typeof AGENT_FILTERS)[number])
            }
            className="ml-auto rounded border border-edge bg-panel-2 px-1 py-0.5 text-[11px] text-dim"
            aria-label="filter by agent"
          >
            {AGENT_FILTERS.map((name) => (
              <option key={name} value={name}>
                {name}
              </option>
            ))}
          </select>
        </PanelHeader>
        <div className="min-h-0 flex-1 overflow-y-auto" data-testid="run-list">
          {runs === null && <SkeletonRows rows={6} />}
          {runs !== null && runs.length === 0 && (
            <EmptyState>No runs yet — ask something in Chat and its trace lands here.</EmptyState>
          )}
          {runs?.map((item) => (
            <button
              key={item.id}
              type="button"
              onClick={() => {
                setSelectedSpan(null);
                router.push(`/runs/${item.id}`);
              }}
              className={`block w-full border-b border-edge/60 px-3 py-2 text-left hover:bg-panel-2/60 ${
                item.id === runId ? "bg-panel-2" : ""
              }`}
            >
              <div className="flex items-center gap-2">
                <AgentBadge agent={item.agent} />
                <StatusPill status={item.status} />
                <span className="mono ml-auto text-[10px] text-faint">{shortId(item.id)}</span>
              </div>
              <div className="mt-1 flex justify-between text-[10px] text-faint">
                <Cost value={item.cost_usd} className="text-[10px]" />
                <span>{ago(item.started_at)}</span>
              </div>
            </button>
          ))}
        </div>
      </aside>

      <section className="flex min-w-0 flex-1 flex-col">
        {runId === null || run === null ? (
          <EmptyState>
            {runId === null
              ? "Select a run to inspect its span tree."
              : "Loading run…"}
          </EmptyState>
        ) : (
          <>
            <RunHeader run={run} live={live} spans={spans} />
            <div className="min-h-0 flex-1 overflow-y-auto p-2">
              <SpanTree
                spans={spans}
                selectedId={selected?.id ?? null}
                onSelect={setSelectedSpan}
              />
            </div>
          </>
        )}
      </section>

      {selected !== null && (
        <SpanDetail span={selected} onClose={() => setSelectedSpan(null)} />
      )}
    </div>
  );
}
