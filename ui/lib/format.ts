/** Display formatting. Money arrives as decimal strings and is formatted by string
 *  manipulation — the UI never routes an amount through a float (invariant 1).
 *
 *  Input is `unknown` on purpose: allocation `line_detail` and flag payloads are
 *  agent-authored JSONB served verbatim, and live agents write JSON numbers (or
 *  omit keys) where the demo scripts write decimal strings. Numbers are stringified
 *  for display only — never parsed, never computed with. */

/** "21568.240000" → "21,568.24"; keeps sign; trims to `places` decimals.
 *  null/undefined/"" (and non-scalar garbage) → "—"; a value that isn't a plain
 *  decimal renders as-is rather than crashing the view. */
export function money(value: unknown, places = 2): string {
  if (typeof value !== "string" && typeof value !== "number") return "—";
  const trimmed = typeof value === "number" ? String(value) : value.trim();
  if (trimmed === "") return "—";
  let raw = trimmed;
  let sign = "";
  if (raw.startsWith("-")) {
    sign = "−";
    raw = raw.slice(1);
  }
  if (!/^\d+(\.\d+)?$/.test(raw)) return trimmed;
  const [wholeRaw, fracRaw = ""] = raw.split(".");
  const whole = wholeRaw.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  const frac = (fracRaw + "0".repeat(places)).slice(0, places);
  return places > 0 ? `${sign}${whole}.${frac}` : `${sign}${whole}`;
}

/** Costs are small: show 4 decimals under $1, cents otherwise. */
export function cost(value: unknown): string {
  if (typeof value !== "string" && typeof value !== "number") return "$0.00";
  const raw = typeof value === "number" ? String(value) : value.trim();
  if (raw === "") return "$0.00";
  const negative = raw.startsWith("-");
  const abs = raw.replace("-", "");
  const underOne = /^0*\./.test(abs) || abs.replace(/\..*$/, "").replace(/^0+/, "") === "";
  return `${negative ? "−" : ""}$${money(abs, underOne ? 4 : 2)}`;
}

export function formatInt(value: number): string {
  return value.toLocaleString("en-US");
}

/** ISO timestamp → "2026-08-06 14:32" (UTC, mono-friendly). */
export function timestamp(iso: string | null | undefined): string {
  if (!iso) return "—";
  const date = new Date(iso);
  const pad = (n: number) => String(n).padStart(2, "0");
  return (
    `${date.getUTCFullYear()}-${pad(date.getUTCMonth() + 1)}-${pad(date.getUTCDate())} ` +
    `${pad(date.getUTCHours())}:${pad(date.getUTCMinutes())}`
  );
}

/** Relative hint ("4m ago") — secondary only, never the primary timestamp. */
export function ago(iso: string | null | undefined): string {
  if (!iso) return "";
  const seconds = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (seconds < 60) return "just now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86400)}d ago`;
}

export function durationMs(start: string, end: string | null): string {
  if (!end) return "…";
  const ms = new Date(end).getTime() - new Date(start).getTime();
  if (ms < 1000) return `${ms}ms`;
  return `${(ms / 1000).toFixed(1)}s`;
}

/** Short id for display: first 8 chars of a UUID. */
export function shortId(id: string): string {
  return id.slice(0, 8);
}
