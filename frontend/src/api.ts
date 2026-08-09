import type { ChatResponse } from "./types";

const BASE = import.meta.env.VITE_API_URL ?? "http://localhost:8000";

export async function sendChat(question: string): Promise<ChatResponse> {
  const res = await fetch(`${BASE}/chat`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ question }),
  });
  if (!res.ok) throw new Error(`API ${res.status}`);
  return res.json();
}
