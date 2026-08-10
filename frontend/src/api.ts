import type { ChatResponse, LoginResponse } from "./types";
import { clearAuth, getAuth } from "./auth";

const BASE = import.meta.env.VITE_API_URL ?? "http://localhost:8000";

export class AuthExpiredError extends Error {}

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
  if (!res.ok) throw new Error(`API ${res.status}`);
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
