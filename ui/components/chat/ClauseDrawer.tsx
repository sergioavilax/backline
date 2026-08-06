"use client";

/** Citation chip → source clause drawer (§6 surface 1). Citations are structural
 *  (FBR-C-00501 §3), so the drawer fetches the exact clause text the agent read. */

import { useEffect, useState } from "react";
import { ApiError, apiGet } from "@/lib/api";
import type { Clause } from "@/lib/types";
import { PanelHeader, Skeleton } from "@/components/ui";

export function ClauseDrawer({
  citation,
  onClose,
}: {
  citation: string; // "FBR-C-00501 §3"
  onClose: () => void;
}) {
  const [clause, setClause] = useState<Clause | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const [code, clauseNo] = citation.split(/\s+/, 2);
    setClause(null);
    setError(null);
    apiGet<Clause>(`/catalog/clauses/${code}/${encodeURIComponent(clauseNo ?? "")}`)
      .then(setClause)
      .catch((err: unknown) =>
        setError(err instanceof ApiError ? err.message : "failed to load clause"),
      );
  }, [citation]);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <aside
      className="drawer-in flex w-[420px] shrink-0 flex-col border-l border-edge bg-panel-2"
      data-testid="clause-drawer"
    >
      <PanelHeader>
        <span className="mono text-[12px] text-text">{citation}</span>
        {clause?.stage_name && <span className="text-[12px] text-dim">· {clause.stage_name}</span>}
        <button
          type="button"
          onClick={onClose}
          className="ml-auto rounded px-1.5 text-dim hover:text-text"
          aria-label="close clause drawer"
        >
          ✕
        </button>
      </PanelHeader>
      <div className="min-h-0 flex-1 overflow-y-auto p-4">
        {error !== null && <div className="text-[12px] text-red">{error}</div>}
        {error === null && clause === null && (
          <div className="flex flex-col gap-2">
            <Skeleton className="h-5 w-2/3" />
            <Skeleton className="h-40" />
          </div>
        )}
        {clause !== null && (
          <>
            <div className="mb-1 text-[14px] font-medium">{clause.heading}</div>
            <div className="label mb-3">
              {clause.kind} · effective {clause.effective_from ?? "—"}
              {clause.effective_to ? ` → ${clause.effective_to}` : ""}
            </div>
            <div className="whitespace-pre-wrap text-[12px] leading-5 text-dim">
              {clause.text}
            </div>
          </>
        )}
      </div>
    </aside>
  );
}
