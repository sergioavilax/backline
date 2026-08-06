/** TS mirror of the API response models (docs/api/openapi.json is the contract).
 *  Every monetary value is a decimal STRING — money is never float, in the UI too:
 *  amounts are formatted by string manipulation (lib/format.ts), never parseFloat. */

export interface Meta {
  version: string;
  demo_mode: boolean;
  providers: string[];
  planner_model: string;
  utility_model: string;
  router_model: string;
  world_seed: number;
}

export interface Session {
  id: string;
  title: string | null;
  created_at: string;
  n_messages: number;
  last_message_at: string | null;
}

export interface Citation {
  ref: string;
  note: string;
}

/** Persisted message content JSONB (assistant turns carry the run linkage). */
export interface MessageContent {
  text: string;
  kind?: "clarify";
  agent?: string;
  run_id?: string;
  status?: string;
  route?: { target: string; confidence: number; reason: string };
  citations?: Citation[];
  abstained?: boolean;
  batch_id?: number | null;
  flags_summary?: string;
  cost_usd?: string;
  iterations?: number;
  demo?: boolean;
}

export interface Message {
  id: string;
  session_id: string;
  role: "user" | "assistant";
  content: MessageContent;
  created_at: string;
}

export interface SessionDetail {
  session: Session;
  messages: Message[];
}

export interface Run {
  id: string;
  session_id: string | null;
  agent: string;
  status: "running" | "completed" | "exhausted" | "error";
  started_at: string;
  finished_at: string | null;
  cost_usd: string;
  meta: Record<string, unknown>;
}

export interface Span {
  id: string;
  run_id: string;
  parent_id: string | null;
  kind: "iteration" | "llm_call" | "tool_call" | "guardrail" | "compression";
  name: string;
  started_at: string;
  ended_at: string | null;
  attrs: Record<string, unknown>;
}

export interface RunDetail {
  run: Run;
  spans: Span[];
}

export interface RunList {
  runs: Run[];
  total: number;
}

export interface Batch {
  id: number;
  period: string;
  status: "proposed" | "approved" | "rejected";
  submitted_by_run: string | null;
  summary: Record<string, unknown> & {
    note?: string;
    review?: { action: string; note: string; at: string };
  };
  created_at: string;
  n_allocations: number;
  n_flags: number;
  total_net_payable: string;
}

export interface Allocation {
  artist_id: number;
  stage_name: string | null;
  period: string;
  net_payable: string;
  line_detail: { gross?: string; recouped?: string; balance_after?: string };
}

export interface Flag {
  id: number;
  kind: string;
  severity: "error" | "warning" | "info" | string;
  payload: Record<string, unknown>;
  evidence: Record<string, unknown>[];
}

export interface PromotionPreview {
  statements_to_promote: {
    id: number;
    distributor: string;
    raw_path: string;
    n_staged_lines: number;
  }[];
  n_staged_lines: number;
  staged_gross_by_currency: Record<string, string>;
  allocation_total: string;
  n_paid_artists: number;
}

export interface BatchDetail {
  batch: Batch;
  allocations: Allocation[];
  flags: Flag[];
  promotion: PromotionPreview;
}

export interface EvalRun {
  id: string;
  suite_hash: string;
  model: string;
  git_sha: string | null;
  started_at: string;
  finished_at: string | null;
  summary: Record<string, unknown> & {
    track?: string;
    subset?: string;
    categories?: Record<
      string,
      { n?: number; score?: number; t1?: number | null; t2?: number | null; t3?: number | null }
    >;
    overall_score?: number;
    n_questions?: number;
    total_cost_usd?: string;
    t2_violations?: number;
  };
}

export interface EvalResult {
  question_id: string;
  tier: "t1" | "t2" | "t3";
  score: string | null;
  passed: boolean | null;
  detail: Record<string, unknown> & {
    category?: string;
    agent?: string;
    run_id?: string | null;
    expected?: unknown;
    answer?: unknown;
    cost_usd?: string;
  };
}

export interface EvalRunDetail {
  run: EvalRun;
  results: EvalResult[];
}

export interface Baseline {
  baselines: {
    model: string;
    track: string;
    subset: string;
    suite_hash: string;
    git_sha: string;
    recorded_at: string;
    note: string;
    categories: Record<string, number>;
  }[];
}

export interface Artist {
  id: number;
  stage_name: string;
  legal_name: string;
  joined_at: string;
  n_tracks: number;
  n_releases: number;
  n_contracts: number;
}

export interface Clause {
  code: string;
  contract_id: number;
  clause_no: string;
  heading: string | null;
  text: string;
  artist_id: number | null;
  stage_name: string | null;
  kind: string;
  effective_from: string | null;
  effective_to: string | null;
}

/** Chat SSE event payloads (backline/api/chat.py protocol). */
export interface RoutedEvent {
  target: string;
  confidence: number;
  reason: string;
  artists: string[];
  demo: boolean;
}

export interface FinalEvent {
  run_id: string;
  agent: string;
  status: string;
  iterations: number;
  cost_usd: string;
  demo: boolean;
  text: string;
  citations: Citation[];
  abstained: boolean;
  batch_id: number | null;
  flags_summary: string;
}
