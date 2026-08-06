/** API client: plain fetch against the Backline API + SSE readers.
 *
 *  The browser talks to the API origin directly (compose publishes it on :8000);
 *  override with NEXT_PUBLIC_API_URL at build time. SSE for POST bodies uses
 *  fetch + ReadableStream (EventSource is GET-only). */

export const API_URL =
  process.env.NEXT_PUBLIC_API_URL?.replace(/\/$/, "") ?? "http://localhost:8000";

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

async function parseError(response: Response): Promise<ApiError> {
  let detail = response.statusText;
  try {
    const body = await response.json();
    if (typeof body.detail === "string") detail = body.detail;
    else if (body.detail) detail = JSON.stringify(body.detail);
  } catch {
    /* non-JSON error body */
  }
  return new ApiError(response.status, detail);
}

export async function apiGet<T>(path: string): Promise<T> {
  const response = await fetch(`${API_URL}${path}`, { cache: "no-store" });
  if (!response.ok) throw await parseError(response);
  return response.json();
}

export async function apiPost<T>(path: string, body: unknown): Promise<T> {
  const response = await fetch(`${API_URL}${path}`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!response.ok) throw await parseError(response);
  return response.json();
}

export interface SseEvent {
  event: string;
  data: unknown;
}

/** POST + read the SSE reply frame by frame (chat). */
export async function* postSse(
  path: string,
  body: unknown,
  signal?: AbortSignal,
): AsyncGenerator<SseEvent> {
  const response = await fetch(`${API_URL}${path}`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });
  if (!response.ok || response.body === null) throw await parseError(response);
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let eventName: string | null = null;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) return;
    buffer += decoder.decode(value, { stream: true });
    let newline;
    while ((newline = buffer.indexOf("\n")) >= 0) {
      const line = buffer.slice(0, newline).replace(/\r$/, "");
      buffer = buffer.slice(newline + 1);
      if (line.startsWith("event: ")) {
        eventName = line.slice(7);
      } else if (line.startsWith("data: ") && eventName !== null) {
        yield { event: eventName, data: JSON.parse(line.slice(6)) };
        eventName = null;
      }
      // blank lines and ": keepalive" comments fall through
    }
  }
}

/** GET SSE via EventSource (span stream). Returns the unsubscribe function. */
export function openEventStream(
  path: string,
  events: string[],
  onEvent: (event: string, data: unknown) => void,
  onEnd?: () => void,
): () => void {
  const source = new EventSource(`${API_URL}${path}`);
  for (const name of events) {
    source.addEventListener(name, (raw) => {
      onEvent(name, JSON.parse((raw as MessageEvent).data));
    });
  }
  source.onerror = () => {
    // The server closes the stream after run_end; EventSource reports that as an
    // error. Treat close as end-of-feed; the caller decides whether to reopen.
    source.close();
    onEnd?.();
  };
  return () => source.close();
}
