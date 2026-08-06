"use client";

/** One chat turn. Assistant turns carry the routing badge, citations as chips,
 *  batch links, and the trace link; abstentions render as a distinct quiet state. */

import Link from "next/link";
import type { MessageContent } from "@/lib/types";
import { AgentBadge, Cost } from "@/components/ui";

export function RouteBadge({
  target,
  confidence,
}: {
  target: string;
  confidence: number;
}) {
  return (
    <span className="label inline-flex items-center gap-1" data-testid="route-badge">
      routed to <span className="text-text normal-case">{target}</span> ·{" "}
      <span className="mono">{confidence.toFixed(2)}</span>
    </span>
  );
}

export function CitationChips({
  citations,
  onOpen,
}: {
  citations: { ref: string }[];
  onOpen: (ref: string) => void;
}) {
  if (citations.length === 0) return null;
  return (
    <div className="mt-2 flex flex-wrap gap-1.5">
      {citations.map((citation) => (
        <button
          key={citation.ref}
          type="button"
          onClick={() => onOpen(citation.ref)}
          data-testid="citation-chip"
          className="mono rounded-[4px] border border-edge-strong bg-panel-2 px-1.5 py-0.5 text-[11px] text-blue hover:border-blue/60"
        >
          {citation.ref}
        </button>
      ))}
    </div>
  );
}

export function MessageBubble({
  role,
  content,
  onOpenCitation,
}: {
  role: "user" | "assistant";
  content: MessageContent;
  onOpenCitation: (ref: string) => void;
}) {
  if (role === "user") {
    return (
      <div className="ml-auto max-w-[70%] rounded-[6px] bg-panel-2 px-3 py-2 text-[13px]">
        <div className="whitespace-pre-wrap">{content.text}</div>
      </div>
    );
  }

  const clarify = content.kind === "clarify";
  const abstained = content.abstained === true;
  return (
    <div
      className={`max-w-[85%] rounded-[6px] border px-3 py-2 text-[13px] ${
        abstained
          ? "border-edge bg-transparent text-dim"
          : clarify
            ? "border-edge bg-transparent"
            : "border-edge bg-panel"
      }`}
      data-testid={abstained ? "message-abstained" : "message-assistant"}
    >
      <div className="mb-1.5 flex flex-wrap items-center gap-2">
        {content.route && !clarify && (
          <RouteBadge target={content.route.target} confidence={content.route.confidence} />
        )}
        {clarify && <span className="label">needs clarification</span>}
        {abstained && <span className="label border border-edge px-1 py-px">abstained</span>}
        {content.demo === true && <span className="label text-amber/70">demo script</span>}
      </div>
      <div className="whitespace-pre-wrap leading-5">{content.text}</div>
      {content.citations && (
        <CitationChips citations={content.citations} onOpen={onOpenCitation} />
      )}
      {(content.run_id || content.batch_id != null) && (
        <div className="mt-2 flex flex-wrap items-center gap-3 border-t border-edge pt-1.5 text-[11px] text-faint">
          {content.agent && <AgentBadge agent={content.agent} />}
          {content.cost_usd && <Cost value={content.cost_usd} className="text-[11px]" />}
          {typeof content.iterations === "number" && (
            <span className="mono">{content.iterations} it</span>
          )}
          {content.batch_id != null && (
            <Link
              href="/review"
              className="text-amber hover:underline"
              data-testid="batch-link"
            >
              batch #{content.batch_id} → review
            </Link>
          )}
          {content.run_id && (
            <Link
              href={`/runs/${content.run_id}`}
              className="ml-auto text-blue hover:underline"
              data-testid="trace-link"
            >
              view trace →
            </Link>
          )}
        </div>
      )}
    </div>
  );
}
