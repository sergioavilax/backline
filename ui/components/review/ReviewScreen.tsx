"use client";

/** Review Queue (§6 surface 3): the human half of invariant 5.
 *
 *  Keyboard-first: j/k navigate batches, a approves, r opens the rejection note
 *  (required — the API refuses an empty note), Esc cancels. The detail pane shows
 *  proposed allocations, flags grouped by severity with linked evidence lines, and
 *  the diff-style "what changes if approved" promotion preview. */

import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, apiGet, apiPost } from "@/lib/api";
import { ago, money, timestamp } from "@/lib/format";
import type { Batch, BatchDetail } from "@/lib/types";
import {
  EmptyState,
  Money,
  PanelHeader,
  SeverityPill,
  SkeletonRows,
  StatusPill,
} from "@/components/ui";

type StatusFilter = "proposed" | "all";

/** Flag payloads are agent-authored JSONB, served verbatim — live agents shape them
 *  freely (demo scripts always wrote {source, line_id, statement_id, detail}).
 *  Rendering tolerates every shape: numeric-string ids, line_ids lists, object
 *  details, missing or null anything. */

const cell = (value: unknown): string =>
  value === null || value === undefined || value === "" ? "—" : String(value);

/** One-line text for an arbitrary payload value; "" for null/undefined. */
function describe(value: unknown): string {
  if (value === null || value === undefined) return "";
  if (typeof value === "object") {
    try {
      return JSON.stringify(value);
    } catch {
      return String(value);
    }
  }
  return String(value);
}

/** "label:4021" / "staged:88101 +2" from line_id and/or line_ids, if any. */
function lineRef(payload: Record<string, unknown>): string | null {
  const raw = payload["line_ids"];
  const ids = [payload["line_id"], ...(Array.isArray(raw) ? raw : [raw])].filter(
    (v): v is number | string =>
      typeof v === "number" || (typeof v === "string" && /^\d+$/.test(v)),
  );
  if (ids.length === 0) return null;
  const source = payload["source"] === "staged" ? "staged" : "label";
  return `${source}:${ids[0]}${ids.length > 1 ? ` +${ids.length - 1}` : ""}`;
}

const SHOWN_PAYLOAD_KEYS = new Set(["source", "line_id", "line_ids", "statement_id", "detail"]);

/** Payload entries not already rendered elsewhere on the card (agent-written
 *  measurements and context) as "key=value" pairs. */
function extraPayload(payload: Record<string, unknown>): string[] {
  return Object.entries(payload)
    .filter(([key, value]) => !SHOWN_PAYLOAD_KEYS.has(key) && describe(value) !== "")
    .map(([key, value]) => `${key}=${describe(value)}`);
}

function PromotionPanel({ detail }: { detail: BatchDetail }) {
  const { promotion, batch } = detail;
  return (
    <div className="rounded-[6px] border border-edge bg-panel p-3" data-testid="promotion-panel">
      <div className="label mb-2">what changes if approved</div>
      <ul className="flex flex-col gap-1 text-[12px] text-dim">
        <li>
          <span className="text-green">+</span> batch #{batch.id} →{" "}
          <span className="text-green">approved</span>; allocations for{" "}
          <span className="mono">{promotion.n_paid_artists}</span> artists totalling{" "}
          <Money value={promotion.allocation_total} className="text-green" /> USD become the
          period&apos;s payable record
        </li>
        {promotion.statements_to_promote.length > 0 ? (
          <>
            <li>
              <span className="text-green">+</span>{" "}
              <span className="mono">{promotion.n_staged_lines.toLocaleString()}</span> staged
              lines promote into <span className="mono">label.statement_lines</span>
            </li>
            {promotion.statements_to_promote.map((statement) => (
              <li key={statement.id} className="pl-4">
                <span className="mono">{statement.raw_path}</span> ({statement.distributor}) →{" "}
                <span className="text-green">ingested</span>
              </li>
            ))}
            {Object.entries(promotion.staged_gross_by_currency).map(([currency, gross]) => (
              <li key={currency} className="pl-4">
                staged gross <span className="mono">{currency}</span>{" "}
                <Money value={gross} />
              </li>
            ))}
          </>
        ) : (
          <li>
            <span className="text-faint">·</span> no staged lines to promote — the period&apos;s
            statements are already ingested
          </li>
        )}
        <li>
          <span className="text-faint">·</span> nothing else moves: rejected batches leave label
          state untouched
        </li>
      </ul>
    </div>
  );
}

export function ReviewScreen() {
  const [filter, setFilter] = useState<StatusFilter>("proposed");
  const [batches, setBatches] = useState<Batch[] | null>(null);
  const [cursor, setCursor] = useState(0);
  const [detail, setDetail] = useState<BatchDetail | null>(null);
  const [rejecting, setRejecting] = useState(false);
  const [note, setNote] = useState("");
  const [flash, setFlash] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const noteRef = useRef<HTMLTextAreaElement | null>(null);

  const load = useCallback(async () => {
    const status = filter === "proposed" ? "proposed" : "all";
    const rows = await apiGet<Batch[]>(`/review/batches?status=${status}`);
    setBatches(rows);
    setCursor((position) => Math.min(position, Math.max(0, rows.length - 1)));
  }, [filter]);

  useEffect(() => {
    setBatches(null);
    load().catch(() => setBatches([]));
  }, [load]);

  const selected = batches?.[cursor] ?? null;

  useEffect(() => {
    setDetail(null);
    setRejecting(false);
    setNote("");
    setError(null);
    if (selected === null) return;
    apiGet<BatchDetail>(`/review/batches/${selected.id}`)
      .then(setDetail)
      .catch(() => setDetail(null));
  }, [selected?.id]); // eslint-disable-line react-hooks/exhaustive-deps

  const act = useCallback(
    async (action: "approve" | "reject") => {
      if (selected === null || selected.status !== "proposed") return;
      if (action === "reject" && note.trim() === "") {
        setRejecting(true);
        noteRef.current?.focus();
        return;
      }
      setError(null);
      try {
        await apiPost<Batch>(`/review/batches/${selected.id}/${action}`, {
          note: note.trim(),
        });
        setFlash(`batch #${selected.id} ${action === "approve" ? "approved" : "rejected"}`);
        setTimeout(() => setFlash(null), 2500);
        setRejecting(false);
        setNote("");
        await load();
      } catch (err) {
        setError(err instanceof ApiError ? err.message : "action failed");
      }
    },
    [selected, note, load],
  );

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement;
      if (target.tagName === "TEXTAREA" || target.tagName === "INPUT") {
        if (event.key === "Escape") {
          setRejecting(false);
          (target as HTMLTextAreaElement).blur();
        }
        return;
      }
      if (event.key === "j") setCursor((c) => Math.min(c + 1, (batches?.length ?? 1) - 1));
      else if (event.key === "k") setCursor((c) => Math.max(c - 1, 0));
      else if (event.key === "a") void act("approve");
      else if (event.key === "r") {
        setRejecting(true);
        setTimeout(() => noteRef.current?.focus(), 0);
      } else if (event.key === "Escape") setRejecting(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [act, batches]);

  return (
    <div className="flex h-screen">
      <aside className="flex w-[300px] shrink-0 flex-col border-r border-edge bg-panel">
        <PanelHeader>
          <span className="label">Review Queue</span>
          <select
            value={filter}
            onChange={(event) => setFilter(event.target.value as StatusFilter)}
            className="ml-auto rounded border border-edge bg-panel-2 px-1 py-0.5 text-[11px] text-dim"
            aria-label="filter batches"
          >
            <option value="proposed">proposed</option>
            <option value="all">all</option>
          </select>
        </PanelHeader>
        <div className="min-h-0 flex-1 overflow-y-auto" data-testid="batch-list">
          {batches === null && <SkeletonRows rows={4} />}
          {batches !== null && batches.length === 0 && (
            <EmptyState>
              Nothing waiting for review. Ask the Reconciler to process a statement period in
              Chat.
            </EmptyState>
          )}
          {batches?.map((batch, index) => (
            <button
              key={batch.id}
              type="button"
              onClick={() => setCursor(index)}
              data-testid={`batch-row-${batch.id}`}
              className={`block w-full border-b border-edge/60 px-3 py-2 text-left hover:bg-panel-2/60 ${
                index === cursor ? "bg-panel-2" : ""
              }`}
            >
              <div className="flex items-center gap-2">
                <span className="mono text-[12px]">#{batch.id}</span>
                <span className="mono text-[12px] text-dim">{batch.period}</span>
                <StatusPill status={batch.status} />
              </div>
              <div className="mt-1 flex justify-between text-[10px] text-faint">
                <span className="mono">
                  {batch.n_allocations} alloc · {batch.n_flags} flags
                </span>
                <span>{ago(batch.created_at)}</span>
              </div>
              <div className="mono mt-0.5 text-[11px] text-dim">
                {money(batch.total_net_payable)} USD
              </div>
            </button>
          ))}
        </div>
        <div className="label border-t border-edge px-3 py-2 text-[10px]">
          j/k navigate · a approve · r reject · esc cancel
        </div>
      </aside>

      <section className="flex min-w-0 flex-1 flex-col overflow-y-auto">
        {flash !== null && (
          <div className="border-b border-green/40 bg-green/10 px-4 py-2 text-[12px] text-green">
            {flash}
          </div>
        )}
        {error !== null && (
          <div className="border-b border-red/40 bg-red/10 px-4 py-2 text-[12px] text-red">
            {error}
          </div>
        )}
        {selected === null || detail === null ? (
          <EmptyState>
            {batches !== null && batches.length === 0
              ? "The queue is clear."
              : "Loading batch…"}
          </EmptyState>
        ) : (
          <div className="flex flex-col gap-4 p-4" data-testid="batch-detail">
            {/* Header */}
            <div className="flex items-center gap-3">
              <h1 className="text-[20px] font-medium">
                Batch <span className="mono">#{detail.batch.id}</span>
              </h1>
              <span className="mono text-dim">{detail.batch.period}</span>
              <StatusPill status={detail.batch.status} />
              <span className="mono ml-auto text-[15px]">
                {money(detail.batch.total_net_payable)} USD
              </span>
            </div>
            <div className="flex flex-wrap gap-x-4 gap-y-1 text-[11px] text-dim">
              <span>
                submitted <span className="mono">{timestamp(detail.batch.created_at)}</span>
              </span>
              {detail.batch.submitted_by_run && (
                <a
                  href={`/runs/${detail.batch.submitted_by_run}`}
                  className="text-blue hover:underline"
                >
                  proposing run →
                </a>
              )}
              {detail.batch.summary.review && (
                <span>
                  reviewed: {detail.batch.summary.review.action} —{" "}
                  {detail.batch.summary.review.note || "no note"}
                </span>
              )}
            </div>
            {typeof detail.batch.summary.note === "string" &&
              detail.batch.summary.note !== "" && (
                <div className="rounded-[6px] border border-edge bg-panel p-3 text-[12px] leading-5 text-dim">
                  {detail.batch.summary.note}
                </div>
              )}

            {/* Actions */}
            {detail.batch.status === "proposed" && (
              <div className="flex items-start gap-2">
                <button
                  type="button"
                  onClick={() => void act("approve")}
                  data-testid="approve"
                  className="rounded-[6px] border border-green/50 px-4 py-1.5 text-[13px] text-green hover:bg-green/10"
                >
                  Approve <span className="label ml-1">a</span>
                </button>
                <button
                  type="button"
                  onClick={() => {
                    setRejecting(true);
                    setTimeout(() => noteRef.current?.focus(), 0);
                  }}
                  data-testid="reject"
                  className="rounded-[6px] border border-red/50 px-4 py-1.5 text-[13px] text-red hover:bg-red/10"
                >
                  Reject <span className="label ml-1">r</span>
                </button>
                {rejecting && (
                  <div className="flex flex-1 items-start gap-2" data-testid="reject-note">
                    <textarea
                      ref={noteRef}
                      value={note}
                      onChange={(event) => setNote(event.target.value)}
                      rows={2}
                      placeholder="Why is this batch wrong? (required)"
                      className="flex-1 rounded-[6px] border border-red/40 bg-panel px-3 py-1.5 text-[12px] placeholder:text-faint"
                    />
                    <button
                      type="button"
                      onClick={() => void act("reject")}
                      disabled={note.trim() === ""}
                      data-testid="confirm-reject"
                      className="rounded-[6px] border border-red/50 px-3 py-1.5 text-[12px] text-red disabled:opacity-40"
                    >
                      Confirm
                    </button>
                  </div>
                )}
              </div>
            )}

            <PromotionPanel detail={detail} />

            {/* Flags */}
            <div>
              <div className="label mb-2">
                flags <span className="mono">({detail.flags.length})</span>
              </div>
              {detail.flags.length === 0 ? (
                <div className="text-[12px] text-faint">
                  No flags raised — nothing out of tolerance this period.
                </div>
              ) : (
                <div className="flex flex-col gap-2">
                  {detail.flags.map((flag) => (
                    <div
                      key={flag.id}
                      className="rounded-[6px] border border-edge bg-panel p-3"
                      data-testid="flag-card"
                    >
                      <div className="flex items-center gap-2">
                        <SeverityPill severity={flag.severity} />
                        <span className="mono text-[12px]">{flag.kind}</span>
                        {lineRef(flag.payload) !== null && (
                          <span className="mono ml-auto text-[10px] text-faint">
                            {lineRef(flag.payload)}
                          </span>
                        )}
                      </div>
                      {describe(flag.payload["detail"]) !== "" && (
                        <div className="mt-1 text-[12px] text-dim">
                          {describe(flag.payload["detail"])}
                        </div>
                      )}
                      {extraPayload(flag.payload).length > 0 && (
                        <div className="mono mt-1 text-[11px] text-faint">
                          {extraPayload(flag.payload).join(" · ")}
                        </div>
                      )}
                      {flag.evidence.length > 0 && (
                        <table className="mono mt-2 w-full text-[11px] text-dim">
                          <thead>
                            <tr className="label text-left">
                              <th className="py-1 pr-2 font-normal">line</th>
                              <th className="py-1 pr-2 font-normal">isrc</th>
                              <th className="py-1 pr-2 font-normal">store</th>
                              <th className="py-1 pr-2 font-normal">terr</th>
                              <th className="py-1 pr-2 text-right font-normal">units</th>
                              <th className="py-1 text-right font-normal">gross</th>
                            </tr>
                          </thead>
                          <tbody>
                            {flag.evidence.map((line, index) => (
                              <tr key={index} className="border-t border-edge/60">
                                <td className="py-1 pr-2">{cell(line["id"])}</td>
                                <td className="py-1 pr-2">{cell(line["isrc"])}</td>
                                <td className="py-1 pr-2">{cell(line["store"])}</td>
                                <td className="py-1 pr-2">{cell(line["territory"])}</td>
                                <td className="py-1 pr-2 text-right">
                                  {cell(line["units"])}
                                </td>
                                <td className="py-1 text-right">
                                  {money(line["gross_amount"])} {cell(line["currency"])}
                                </td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      )}
                    </div>
                  ))}
                </div>
              )}
            </div>

            {/* Allocations */}
            <div>
              <div className="label mb-2">
                proposed allocations <span className="mono">({detail.allocations.length})</span>
              </div>
              <table className="w-full text-[12px]" data-testid="allocations-table">
                <thead>
                  <tr className="label text-left">
                    <th className="border-b border-edge py-1.5 pr-2 font-normal">artist</th>
                    <th className="border-b border-edge py-1.5 pr-2 text-right font-normal">
                      gross
                    </th>
                    <th className="border-b border-edge py-1.5 pr-2 text-right font-normal">
                      recouped
                    </th>
                    <th className="border-b border-edge py-1.5 pr-2 text-right font-normal">
                      balance after
                    </th>
                    <th className="border-b border-edge py-1.5 text-right font-normal">
                      net payable
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {detail.allocations.map((allocation) => (
                    <tr
                      key={allocation.artist_id}
                      className="border-b border-edge/60 hover:bg-panel-2/60"
                    >
                      <td className="py-1.5 pr-2">
                        {allocation.stage_name ?? `artist ${allocation.artist_id}`}
                        <span className="mono ml-1.5 text-[10px] text-faint">
                          #{allocation.artist_id}
                        </span>
                      </td>
                      <td className="mono py-1.5 pr-2 text-right text-dim">
                        {money(allocation.line_detail?.gross)}
                      </td>
                      <td className="mono py-1.5 pr-2 text-right text-dim">
                        {money(allocation.line_detail?.recouped)}
                      </td>
                      <td className="mono py-1.5 pr-2 text-right text-dim">
                        {money(allocation.line_detail?.balance_after)}
                      </td>
                      <td className="mono py-1.5 text-right text-green">
                        {money(allocation.net_payable)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        )}
      </section>
    </div>
  );
}
