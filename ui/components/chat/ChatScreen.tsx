"use client";

/** Chat (§6 surface 1): session list · streaming conversation · live run panel.
 *
 *  A turn streams over SSE: `routed` paints the badge immediately, `run_started`
 *  opens the live span timeline inside the pending bubble (the signature element,
 *  fed by the same stream the Trace Inspector uses), `final` replaces it with the
 *  answer. Abstentions and clarifications render as quiet states, not errors. */

import { useCallback, useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { apiGet, apiPost, postSse } from "@/lib/api";
import { ago, cost as formatCost } from "@/lib/format";
import type {
  FinalEvent,
  Message,
  MessageContent,
  RoutedEvent,
  Session,
  SessionDetail,
} from "@/lib/types";
import { useSpanStream } from "@/lib/useSpanStream";
import { SpanTree } from "@/components/SpanTree";
import { AgentBadge, EmptyState, PanelHeader, SkeletonRows } from "@/components/ui";
import { ClauseDrawer } from "./ClauseDrawer";
import { MessageBubble, RouteBadge } from "./MessageBubble";

interface PendingTurn {
  route: RoutedEvent | null;
  runId: string | null;
  agent: string | null;
}

function LiveRunPanel({ runId, agent }: { runId: string; agent: string }) {
  const { spans, live } = useSpanStream(runId);
  const total = spans
    .filter((span) => typeof span.attrs["cost_usd"] === "string")
    .reduce((sum, span) => sum + Number(span.attrs["cost_usd"]), 0);
  return (
    <div
      className="max-w-[85%] rounded-[6px] border border-edge bg-panel px-3 py-2"
      data-testid="live-run-panel"
    >
      <div className="mb-1 flex items-center gap-2">
        <AgentBadge agent={agent} />
        <span className={`label ${live ? "text-amber" : ""}`}>
          {live ? "running" : "finishing"}
        </span>
        {/* Display-only cost tick: floats are fine for a live approximation; the
            exact Decimal figure lands with the final message. */}
        <span className="mono ml-auto text-[11px] text-amber">
          {formatCost(total.toFixed(6))}
        </span>
      </div>
      <SpanTree spans={spans} />
    </div>
  );
}

export function ChatScreen({ sessionId }: { sessionId: string | null }) {
  const router = useRouter();
  const [sessions, setSessions] = useState<Session[] | null>(null);
  const [messages, setMessages] = useState<Message[] | null>(null);
  const [pending, setPending] = useState<PendingTurn | null>(null);
  const [draft, setDraft] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [openCitation, setOpenCitation] = useState<string | null>(null);
  const bottomRef = useRef<HTMLDivElement | null>(null);
  const busy = pending !== null;

  const refreshSessions = useCallback(() => {
    apiGet<Session[]>("/sessions").then(setSessions).catch(() => setSessions([]));
  }, []);

  useEffect(refreshSessions, [refreshSessions]);

  useEffect(() => {
    setError(null);
    setOpenCitation(null);
    if (sessionId === null) {
      setMessages([]);
      return;
    }
    setMessages(null);
    apiGet<SessionDetail>(`/sessions/${sessionId}`)
      .then((detail) => setMessages(detail.messages))
      .catch(() => {
        setMessages([]);
        setError(`session ${sessionId} not found`);
      });
  }, [sessionId]);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ block: "end" });
  }, [messages, pending]);

  const appendLocal = (role: "user" | "assistant", content: MessageContent) => {
    setMessages((previous) => [
      ...(previous ?? []),
      {
        id: `local-${Date.now()}-${role}`,
        session_id: sessionId ?? "",
        role,
        content,
        created_at: new Date().toISOString(),
      },
    ]);
  };

  const send = async () => {
    const text = draft.trim();
    if (text === "" || busy) return;
    setDraft("");
    setError(null);

    let target = sessionId;
    if (target === null) {
      const created = await apiPost<Session>("/sessions", {});
      target = created.id;
      window.history.replaceState(null, "", `/chat/${target}`);
    }
    appendLocal("user", { text });
    setPending({ route: null, runId: null, agent: null });

    try {
      for await (const frame of postSse(`/sessions/${target}/messages`, { text })) {
        if (frame.event === "routed") {
          const route = frame.data as RoutedEvent;
          setPending((prev) => ({ ...(prev ?? { runId: null, agent: null }), route }));
        } else if (frame.event === "run_started") {
          const { run_id, agent } = frame.data as { run_id: string; agent: string };
          setPending((prev) => ({ ...(prev ?? { route: null }), runId: run_id, agent }));
        } else if (frame.event === "clarify") {
          const { question } = frame.data as { question: string };
          appendLocal("assistant", { text: question, kind: "clarify" });
        } else if (frame.event === "final") {
          const final = frame.data as FinalEvent;
          appendLocal("assistant", {
            text: final.text,
            agent: final.agent,
            run_id: final.run_id,
            status: final.status,
            citations: final.citations,
            abstained: final.abstained,
            batch_id: final.batch_id,
            flags_summary: final.flags_summary,
            cost_usd: final.cost_usd,
            iterations: final.iterations,
            demo: final.demo,
            route: pendingRouteRef.current ?? undefined,
          });
        } else if (frame.event === "error") {
          setError(String((frame.data as { detail: string }).detail));
        }
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "message failed");
    } finally {
      setPending(null);
      refreshSessions();
      if (sessionId === null && target !== null) router.replace(`/chat/${target}`);
    }
  };

  // The final event needs the route decision captured earlier in the stream.
  const pendingRouteRef = useRef<{ target: string; confidence: number; reason: string } | null>(
    null,
  );
  useEffect(() => {
    pendingRouteRef.current = pending?.route
      ? {
          target: pending.route.target,
          confidence: pending.route.confidence,
          reason: pending.route.reason,
        }
      : pendingRouteRef.current;
  }, [pending]);

  return (
    <div className="flex h-screen">
      {/* Session rail */}
      <aside className="flex w-64 shrink-0 flex-col border-r border-edge bg-panel">
        <PanelHeader>
          <span className="label">Sessions</span>
          <button
            type="button"
            onClick={() => router.push("/chat")}
            className="ml-auto rounded border border-edge-strong px-1.5 py-0.5 text-[11px] text-dim hover:text-text"
            data-testid="new-session"
          >
            + new
          </button>
        </PanelHeader>
        <div className="min-h-0 flex-1 overflow-y-auto">
          {sessions === null && <SkeletonRows rows={5} />}
          {sessions !== null && sessions.length === 0 && (
            <EmptyState>No sessions yet — ask something below.</EmptyState>
          )}
          {sessions?.map((session) => (
            <button
              key={session.id}
              type="button"
              onClick={() => router.push(`/chat/${session.id}`)}
              className={`block w-full border-b border-edge/60 px-3 py-2 text-left hover:bg-panel-2/60 ${
                session.id === sessionId ? "bg-panel-2" : ""
              }`}
            >
              <div className="truncate text-[12px]">
                {session.title ?? <span className="text-faint">untitled</span>}
              </div>
              <div className="mt-0.5 flex justify-between text-[10px] text-faint">
                <span className="mono">{session.n_messages} msg</span>
                <span>{ago(session.last_message_at ?? session.created_at)}</span>
              </div>
            </button>
          ))}
        </div>
      </aside>

      {/* Conversation */}
      <section className="flex min-w-0 flex-1 flex-col">
        <div className="min-h-0 flex-1 overflow-y-auto px-6 py-4">
          {messages === null ? (
            <SkeletonRows rows={3} />
          ) : messages.length === 0 && pending === null ? (
            <EmptyState>
              <p className="mb-2 text-dim">Ask the platform anything about the label.</p>
              <p>
                “What is Umbra&apos;s streaming rate?” · “Top tracks by revenue in 2026-03” ·
                “Reconcile the 2026-06 statements”
              </p>
            </EmptyState>
          ) : (
            <div className="mx-auto flex max-w-3xl flex-col gap-3">
              {messages.map((message) => (
                <MessageBubble
                  key={message.id}
                  role={message.role}
                  content={message.content}
                  onOpenCitation={setOpenCitation}
                />
              ))}
              {pending !== null && (
                <div className="flex flex-col gap-2" data-testid="pending-turn">
                  {pending.route !== null && (
                    <div>
                      <RouteBadge
                        target={pending.route.target}
                        confidence={pending.route.confidence}
                      />
                    </div>
                  )}
                  {pending.runId !== null && pending.agent !== null ? (
                    <LiveRunPanel runId={pending.runId} agent={pending.agent} />
                  ) : (
                    <div className="text-[12px] text-faint">routing…</div>
                  )}
                </div>
              )}
              <div ref={bottomRef} />
            </div>
          )}
        </div>

        {error !== null && (
          <div className="border-t border-red/40 bg-red/10 px-6 py-2 text-[12px] text-red">
            {error}
          </div>
        )}

        {/* Composer */}
        <div className="shrink-0 border-t border-edge p-4">
          <div className="mx-auto flex max-w-3xl items-end gap-2">
            <textarea
              value={draft}
              onChange={(event) => setDraft(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter" && !event.shiftKey) {
                  event.preventDefault();
                  void send();
                }
              }}
              rows={2}
              placeholder={busy ? "run in progress…" : "Message the platform  (Enter to send)"}
              disabled={busy}
              data-testid="composer"
              className="min-h-[42px] flex-1 resize-none rounded-[6px] border border-edge bg-panel px-3 py-2 text-[13px] placeholder:text-faint focus:border-edge-strong"
            />
            <button
              type="button"
              onClick={() => void send()}
              disabled={busy || draft.trim() === ""}
              data-testid="send"
              className="h-[42px] rounded-[6px] border border-edge-strong bg-panel-2 px-4 text-[13px] text-text disabled:text-faint"
            >
              Send
            </button>
          </div>
        </div>
      </section>

      {openCitation !== null && (
        <ClauseDrawer citation={openCitation} onClose={() => setOpenCitation(null)} />
      )}
    </div>
  );
}
