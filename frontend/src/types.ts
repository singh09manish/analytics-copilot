export interface ChatResponse {
  answer: string;
  sql: string | null;
  columns: string[];
  rows: unknown[][];
  assumptions: string[];
  error_type: string | null;
  retrieval_ms: number;
  intent: string | null;
  request_id: string | null;
  // "vector" (Cortex) or "keyword" (fallback ranking). Optional: older backends
  // omit it.
  retrieval_mode?: string | null;
  // Already emitted by the backend's ChatResponse (agent/pipeline.py); declared
  // here so the instrument strip can render real token telemetry instead of an
  // invented number. Optional because an older backend may omit them -- the UI
  // drops the chip rather than showing a zero it cannot vouch for.
  tokens_in?: number;
  tokens_out?: number;
}

export interface LoginResponse {
  token: string;
  role: "analyst" | "admin";
  email: string;
}

export interface Message {
  role: "user" | "assistant";
  text: string;
  data?: ChatResponse;
  feedback?: "up" | "down";
  feedbackPending?: boolean;
  error?: boolean;
}

// Shapes returned by GET /api/admin/*. Snowflake-cased column names are mapped
// to these lowercase snake_case keys server-side (see backend/src/copilot/api/
// main.py's _rows_to_dicts), so this file is the one place the JSON contract
// needs to match.
export interface AdminOverview {
  total_requests: number;
  error_rate: number;
  p50_latency_ms: number;
  feedback_up: number;
  feedback_down: number;
  by_intent: Record<string, number>;
}

export interface AdminRequestRow {
  request_id: string | null;
  created_at: string | null;
  user_role: string | null;
  intent: string | null;
  status: string | null;
  error_type: string | null;
  e2e_ms: number | null;
  sql_text: string | null;
  question: string | null;
}

export interface AdminFeedbackRow {
  feedback_id: string | null;
  created_at: string | null;
  conversation_id: string | null;
  request_id: string | null;
  rating: string | null;
  comment: string | null;
}
