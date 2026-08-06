/** Small shared primitives: pills, money, skeletons, empty states. */

import type { ReactNode } from "react";
import { cost, money } from "@/lib/format";

export function Money({
  value,
  places = 2,
  signed = false,
  className = "",
}: {
  value: string | null | undefined;
  places?: number;
  signed?: boolean;
  className?: string;
}) {
  const text = money(value, places);
  const positive = signed && value !== null && value !== undefined && !value.startsWith("-");
  return (
    <span className={`mono ${positive ? "text-green" : ""} ${className}`}>
      {positive ? "+" : ""}
      {text}
    </span>
  );
}

export function Cost({ value, className = "" }: { value: string | null | undefined; className?: string }) {
  return <span className={`mono ${className}`}>{cost(value)}</span>;
}

const STATUS_STYLES: Record<string, string> = {
  running: "text-amber border-amber/40",
  completed: "text-dim border-edge",
  approved: "text-green border-green/40",
  proposed: "text-amber border-amber/40",
  rejected: "text-red border-red/40",
  exhausted: "text-red border-red/40",
  error: "text-red border-red/40",
};

export function StatusPill({ status }: { status: string }) {
  const style = STATUS_STYLES[status] ?? "text-dim border-edge";
  return (
    <span
      className={`label inline-block rounded-[3px] border px-1.5 py-px leading-4 ${style}`}
    >
      {status}
    </span>
  );
}

const SEVERITY_STYLES: Record<string, string> = {
  error: "text-red border-red/40",
  warning: "text-amber border-amber/40",
  info: "text-dim border-edge",
};

export function SeverityPill({ severity }: { severity: string }) {
  const style = SEVERITY_STYLES[severity] ?? SEVERITY_STYLES.info;
  return (
    <span className={`label inline-block rounded-[3px] border px-1.5 py-px leading-4 ${style}`}>
      {severity}
    </span>
  );
}

export function AgentBadge({ agent }: { agent: string }) {
  return (
    <span className="label inline-block rounded-[3px] border border-edge-strong bg-panel-2 px-1.5 py-px leading-4 text-text">
      {agent}
    </span>
  );
}

export function Skeleton({ className = "" }: { className?: string }) {
  return <div className={`skeleton ${className}`} aria-hidden />;
}

export function SkeletonRows({ rows = 4 }: { rows?: number }) {
  return (
    <div className="flex flex-col gap-2 p-4">
      {Array.from({ length: rows }, (_, i) => (
        <Skeleton key={i} className="h-6" />
      ))}
    </div>
  );
}

export function EmptyState({ children }: { children: ReactNode }) {
  return (
    <div className="flex h-full min-h-32 items-center justify-center p-8 text-center text-faint">
      <div className="max-w-sm text-[13px] leading-5">{children}</div>
    </div>
  );
}

export function PanelHeader({ children }: { children: ReactNode }) {
  return (
    <div className="flex h-10 shrink-0 items-center gap-2 border-b border-edge px-4">
      {children}
    </div>
  );
}
