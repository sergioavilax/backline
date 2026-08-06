"use client";

/** Live span feed for one run: snapshot → span_start/span_end upserts → run_end.
 *
 *  Upsert rules (the server may replay events): unknown spans insert; an event
 *  with `ended_at` always wins; a start event never downgrades an ended span. */

import { useEffect, useMemo, useState } from "react";
import { openEventStream } from "./api";
import type { Run, Span } from "./types";

export interface SpanStreamState {
  run: Run | null;
  spans: Span[];
  live: boolean; // stream open and run not finished
}

interface SnapshotPayload {
  run: Run;
  spans: Span[];
}

export function useSpanStream(runId: string | null): SpanStreamState {
  const [run, setRun] = useState<Run | null>(null);
  const [spans, setSpans] = useState<Map<string, Span>>(new Map());
  const [live, setLive] = useState(false);

  useEffect(() => {
    if (runId === null) {
      setRun(null);
      setSpans(new Map());
      setLive(false);
      return;
    }
    setRun(null);
    setSpans(new Map());
    setLive(true);

    const upsert = (span: Span) => {
      setSpans((previous) => {
        const existing = previous.get(span.id);
        if (existing && existing.ended_at && !span.ended_at) return previous;
        const next = new Map(previous);
        next.set(span.id, span);
        return next;
      });
    };

    const close = openEventStream(
      `/runs/${runId}/spans/stream`,
      ["snapshot", "span_start", "span_end", "run_end"],
      (event, data) => {
        if (event === "snapshot") {
          const payload = data as SnapshotPayload;
          setRun(payload.run);
          setSpans(new Map(payload.spans.map((span) => [span.id, span])));
        } else if (event === "span_start" || event === "span_end") {
          upsert(data as Span);
        } else if (event === "run_end") {
          setRun(data as Run);
          setLive(false);
        }
      },
      () => setLive(false),
    );
    return close;
  }, [runId]);

  const ordered = useMemo(
    () =>
      [...spans.values()].sort(
        (a, b) =>
          new Date(a.started_at).getTime() - new Date(b.started_at).getTime() ||
          a.id.localeCompare(b.id),
      ),
    [spans],
  );

  return { run, spans: ordered, live };
}
