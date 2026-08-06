"use client";

/** Collapsible JSON viewer for span attrs and tool payloads. Values render in
 *  mono; long strings clamp with expand-on-click. */

import { useState } from "react";

function Value({ value }: { value: unknown }) {
  const [expanded, setExpanded] = useState(false);
  if (typeof value === "string") {
    const long = value.length > 240;
    const shown = expanded || !long ? value : `${value.slice(0, 240)}…`;
    return (
      <span
        className={`whitespace-pre-wrap break-words text-green/80 ${long ? "cursor-pointer" : ""}`}
        onClick={long ? () => setExpanded((v) => !v) : undefined}
        title={long && !expanded ? "click to expand" : undefined}
      >
        “{shown}”
      </span>
    );
  }
  if (value === null) return <span className="text-faint">null</span>;
  if (typeof value === "boolean" || typeof value === "number") {
    return <span className="text-blue">{String(value)}</span>;
  }
  return <span>{String(value)}</span>;
}

function Entry({ name, value, depth }: { name: string; value: unknown; depth: number }) {
  const [open, setOpen] = useState(depth < 2);
  const nested = value !== null && typeof value === "object";
  if (!nested) {
    return (
      <div className="flex gap-2" style={{ paddingLeft: `${depth * 12}px` }}>
        <span className="shrink-0 text-dim">{name}:</span>
        <Value value={value} />
      </div>
    );
  }
  const entries = Array.isArray(value)
    ? value.map((item, index) => [String(index), item] as const)
    : Object.entries(value as Record<string, unknown>);
  return (
    <div>
      <button
        type="button"
        className="flex items-center gap-1 text-dim hover:text-text"
        style={{ paddingLeft: `${depth * 12}px` }}
        onClick={() => setOpen((v) => !v)}
      >
        <span className="w-3 text-faint">{open ? "▾" : "▸"}</span>
        <span>{name}</span>
        <span className="text-faint">
          {Array.isArray(value) ? `[${entries.length}]` : `{${entries.length}}`}
        </span>
      </button>
      {open &&
        entries.map(([key, child]) => (
          <Entry key={key} name={key} value={child} depth={depth + 1} />
        ))}
    </div>
  );
}

export function JsonTree({ data }: { data: Record<string, unknown> }) {
  const entries = Object.entries(data);
  if (entries.length === 0) return <div className="text-faint">empty</div>;
  return (
    <div className="mono flex flex-col gap-0.5 text-[11px] leading-4">
      {entries.map(([key, value]) => (
        <Entry key={key} name={key} value={value} depth={0} />
      ))}
    </div>
  );
}
