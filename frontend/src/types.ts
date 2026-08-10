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
  error?: boolean;
}
