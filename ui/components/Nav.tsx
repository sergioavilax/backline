"use client";

/** Left app rail: the four surfaces + the mode badge (UI_DIRECTION §3). */

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";
import { apiGet } from "@/lib/api";
import type { Meta } from "@/lib/types";

const SURFACES = [
  { href: "/chat", label: "Chat", icon: "❯" },
  { href: "/runs", label: "Traces", icon: "≡" },
  { href: "/review", label: "Review", icon: "✓" },
  { href: "/evals", label: "Evals", icon: "▤" },
];

export function Nav() {
  const pathname = usePathname();
  const [meta, setMeta] = useState<Meta | null>(null);
  useEffect(() => {
    apiGet<Meta>("/meta")
      .then(setMeta)
      .catch(() => setMeta(null));
  }, []);

  return (
    <nav className="flex h-screen w-14 shrink-0 flex-col items-center border-r border-edge bg-panel py-3">
      <Link
        href="/chat"
        className="mono mb-4 flex h-8 w-8 items-center justify-center rounded-[6px] border border-edge-strong text-[13px] font-medium text-amber"
        title="Backline"
      >
        BL
      </Link>
      <div className="flex flex-col gap-1">
        {SURFACES.map((surface) => {
          const active = pathname.startsWith(surface.href);
          return (
            <Link
              key={surface.href}
              href={surface.href}
              title={surface.label}
              className={`flex h-9 w-9 flex-col items-center justify-center rounded-[6px] text-[13px]
                ${active ? "bg-panel-2 text-text" : "text-faint hover:text-dim"}`}
            >
              <span aria-hidden>{surface.icon}</span>
              <span className="text-[7px] uppercase tracking-wider">{surface.label}</span>
            </Link>
          );
        })}
      </div>
      <div className="mt-auto flex flex-col items-center gap-2">
        {meta !== null && meta.demo_mode && (
          <span
            className="label rotate-180 cursor-help py-1 text-[9px] text-amber"
            style={{ writingMode: "vertical-rl" }}
            title="No model provider configured — chat runs deterministic demo scripts through the real platform (tools, traces, review). Set ANTHROPIC_API_KEY for live agents."
            data-testid="demo-badge"
          >
            demo mode
          </span>
        )}
        {meta !== null && !meta.demo_mode && (
          <span
            className="label rotate-180 py-1 text-[9px] text-green"
            style={{ writingMode: "vertical-rl" }}
            title={`Providers: ${meta.providers.join(", ")} · planner ${meta.planner_model}`}
          >
            live
          </span>
        )}
      </div>
    </nav>
  );
}
