export interface ChatResponse {
  answer: string;
  sql: string | null;
  columns: string[];
  rows: unknown[][];
  assumptions: string[];
  error_type: string | null;
  retrieval_ms: number;
}

export interface Message {
  role: "user" | "assistant";
  text: string;
  data?: ChatResponse;
}
