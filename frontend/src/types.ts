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
  // omit it, and the UI does not render it today.
  retrieval_mode?: string | null;
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
