import type {
  AdminFeedbackRow, AdminOverview, AdminRequestRow, ChatResponse, LoginResponse,
} from "./types";
import { clearAuth, getAuth } from "./auth";

// Same-origin in production: CloudFront serves the SPA and proxies /api/* to the
// ALB, so there is no cross-origin request and no CORS preflight. Vite's dev server
// proxies /api to the local backend (see vite.config.ts).
const BASE = "/api";

export class AuthExpiredError extends Error {}

// Carries the HTTP status so callers (e.g. Login) can distinguish a real auth
// rejection (401) from a network/server failure without parsing message text.
export class ApiError extends Error {
  status: number;
  constructor(status: number) {
    super(`API ${status}`);
    this.status = status;
  }
}

async function post<T>(path: string, body: unknown, authed = true): Promise<T> {
  const headers: Record<string, string> = { "content-type": "application/json" };
  if (authed) {
    const a = getAuth();
    if (a) headers["authorization"] = `Bearer ${a.token}`;
  }
  const res = await fetch(`${BASE}${path}`, {
    method: "POST",
    headers,
    body: JSON.stringify(body),
  });
  if (res.status === 401 && authed) {
    clearAuth();
    throw new AuthExpiredError("session expired");
  }
  if (!res.ok) throw new ApiError(res.status);
  return res.json();
}

// GET counterpart to post<T> above: every /api/admin/* route is a read, always
// authenticated (there is no anonymous admin data), so this always attaches the
// bearer header and always treats a 401 as an expired session -- same contract
// as post()'s authed=true path.
async function get<T>(path: string): Promise<T> {
  const headers: Record<string, string> = {};
  const a = getAuth();
  if (a) headers["authorization"] = `Bearer ${a.token}`;
  const res = await fetch(`${BASE}${path}`, { method: "GET", headers });
  if (res.status === 401) {
    clearAuth();
    throw new AuthExpiredError("session expired");
  }
  if (!res.ok) throw new ApiError(res.status);
  return res.json();
}

export const login = (email: string, password: string) =>
  post<LoginResponse>("/auth/login", { email, password }, false);

export const sendChat = (question: string, conversationId: string | null) =>
  post<ChatResponse>("/chat", { question, conversation_id: conversationId });

export const sendFeedback = (requestId: string, conversationId: string | null,
                             rating: "up" | "down", comment?: string) =>
  post<{ status: string }>("/feedback", {
    request_id: requestId, conversation_id: conversationId, rating, comment,
  });

export const getAdminOverview = () => get<AdminOverview>("/admin/overview");

export const getAdminRequests = (limit = 50) =>
  get<AdminRequestRow[]>(`/admin/requests?limit=${limit}`);

export const getAdminFeedback = (limit = 50) =>
  get<AdminFeedbackRow[]>(`/admin/feedback?limit=${limit}`);
