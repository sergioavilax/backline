/** Display formatting. Money arrives as decimal strings and is formatted by string
 *  manipulation — the UI never routes an amount through a float (invariant 1). */

/** "21568.240000" → "21,568.24"; keeps sign; trims to `places` decimals. */
export function money(value: string | null | undefined, places = 2): string {
  if (value === null || value === undefined || value === "") return "—";
  let raw = value.trim();
  let sign = "";
  if (raw.startsWith("-")) {
    sign = "−";
    raw = raw.slice(1);
  }
  const [wholeRaw, fracRaw = ""] = raw.split(".");
  const whole = wholeRaw.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  const frac = (fracRaw + "0".repeat(places)).slice(0, places);
  return places > 0 ? `${sign}${whole}.${frac}` : `${sign}${whole}`;
}

/** Costs are small: show 4 decimals under $1, cents otherwise. */
export function cost(value: string | null | undefined): string {
  if (!value) return "$0.00";
  const negative = value.trim().startsWith("-");
  const abs = value.replace("-", "");
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
