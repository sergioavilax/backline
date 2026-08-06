"use client";

/** The signature element (UI_DIRECTION §4): a vertical span tree that fills in
 *  real time. Active spans pulse amber; completed spans settle to graphite with
 *  duration + cost right-aligned in mono; guardrail spans stay red. */

import { useMemo } from "react";
import { durationMs } from "@/lib/format";
import type { Span } from "@/lib/types";
import { Cost } from "./ui";

interface TreeNode {
  span: Span;
  children: TreeNode[];
}

function buildTree(spans: Span[]): TreeNode[] {
  const nodes = new Map<string, TreeNode>();
  for (const span of spans) nodes.set(span.id, { span, children: [] });
  const roots: TreeNode[] = [];
  for (const node of nodes.values()) {
    const parent = node.span.parent_id ? nodes.get(node.span.parent_id) : undefined;
    if (parent) parent.children.push(node);
    else roots.push(node);
  }
  return roots;
}

const KIND_ICON: Record<Span["kind"], string> = {
  iteration: "▸",
  llm_call: "◈",
  tool_call: "⚒",
  guardrail: "⚑",
  compression: "▽",
};

function spanCost(span: Span): string | null {
  const value = span.attrs["cost_usd"];
  return typeof value === "string" ? value : null;
}

function spanLabel(span: Span): string {
  if (span.kind === "tool_call") return String(span.attrs["tool"] ?? span.name);
  if (span.kind === "guardrail") return String(span.attrs["kind"] ?? span.name);
  return span.name;
}

function SpanRow({
  node,
  depth,
  selectedId,
  onSelect,
}: {
  node: TreeNode;
  depth: number;
  selectedId: string | null;
  onSelect?: (span: Span) => void;
}) {
  const { span } = node;
  const running = span.ended_at === null;
  const guardrail = span.kind === "guardrail";
  const denied = span.attrs["status"] === "denied" || span.attrs["status"] === "error";
  const selected = selectedId === span.id;
  const tokens =
    typeof span.attrs["gen_ai.usage.output_tokens"] === "number"
      ? `${span.attrs["gen_ai.usage.input_tokens"]}→${span.attrs["gen_ai.usage.output_tokens"]}`
      : null;
  return (
    <>
      <button
        type="button"
        onClick={() => onSelect?.(span)}
        data-testid={`span-${span.kind}`}
        className={`span-arrive group flex w-full items-center gap-2 rounded-[3px] py-[3px] pr-2 text-left text-[12px] leading-4
          ${running ? "span-live" : "border-l-2 border-transparent"}
          ${selected ? "bg-panel-2" : "hover:bg-panel-2/60"}`}
        style={{ paddingLeft: `${8 + depth * 14}px` }}
      >
        <span
          className={`w-3 shrink-0 text-center ${
            guardrail || denied ? "text-red" : running ? "text-amber" : "text-faint"
          }`}
        >
          {KIND_ICON[span.kind]}
        </span>
        <span
          className={`truncate ${
            guardrail || denied ? "text-red" : running ? "text-text" : "text-dim"
          }`}
        >
          {spanLabel(span)}
        </span>
        <span className="ml-auto flex shrink-0 items-center gap-2 text-[11px] text-faint">
          {tokens !== null && <span className="mono hidden group-hover:inline">{tokens}</span>}
          {spanCost(span) !== null && <Cost value={spanCost(span)} className="text-[11px]" />}
          <span className="mono w-12 text-right">
            {running ? "live" : durationMs(span.started_at, span.ended_at)}
          </span>
        </span>
      </button>
      {node.children.map((child) => (
        <SpanRow
          key={child.span.id}
          node={child}
          depth={depth + 1}
          selectedId={selectedId}
          onSelect={onSelect}
        />
      ))}
    </>
  );
}

export function SpanTree({
  spans,
  selectedId = null,
  onSelect,
}: {
  spans: Span[];
  selectedId?: string | null;
  onSelect?: (span: Span) => void;
}) {
  const tree = useMemo(() => buildTree(spans), [spans]);
  if (spans.length === 0) {
    return <div className="p-3 text-[12px] text-faint">No spans yet…</div>;
  }
  return (
    <div className="flex flex-col" data-testid="span-tree">
      {tree.map((node) => (
        <SpanRow
          key={node.span.id}
          node={node}
          depth={0}
          selectedId={selectedId}
          onSelect={onSelect}
        />
      ))}
    </div>
  );
}
