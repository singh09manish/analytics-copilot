import { useRef, useState } from "react";
import Admin from "./Admin";
import { AuthExpiredError, sendChat, sendFeedback } from "./api";
import { clearAuth, getAuth, type AuthState } from "./auth";
import Login from "./Login";
import type { Message } from "./types";
import "./App.css";

// crypto.randomUUID() only works in a secure context (HTTPS, or localhost).
// Phase 3 deploys this app to AWS, where an origin could plausibly be served
// non-securely; falling back here means a misconfigured host degrades to a
// manually-built UUID instead of throwing at component-evaluation time and
// white-screening the whole app.
function newConversationId(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  const bytes = typeof crypto !== "undefined" && typeof crypto.getRandomValues === "function"
    ? crypto.getRandomValues(new Uint8Array(16))
    : Uint8Array.from({ length: 16 }, () => Math.floor(Math.random() * 256));
  bytes[6] = (bytes[6] & 0x0f) | 0x40; // version 4
  bytes[8] = (bytes[8] & 0x3f) | 0x80; // variant 10xx
  const hex = Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

export default function App() {
  const [authState, setAuthState] = useState<AuthState | null>(getAuth());
  // Chat is always the default view -- the toggle itself only renders for admins
  // (see the header below), so an analyst never sees or reaches "admin" here.
  const [view, setView] = useState<"chat" | "admin">("chat");
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const bottom = useRef<HTMLDivElement>(null);
  // Lazy ref init (react.dev-recommended pattern): only the first render's
  // assignment sticks, so this generates exactly one id per session.
  const conversationId = useRef<string | null>(null);
  if (conversationId.current === null) conversationId.current = newConversationId();
  // Guards against a fast double-click sending two /feedback POSTs for the
  // same message: unlike Message.feedback (React state, only settles after
  // the request resolves), this ref is set synchronously the instant the
  // first click starts, so a second click in the same tick sees it immediately.
  const pendingFeedback = useRef<Set<number>>(new Set());

  if (!authState) return <Login onLogin={setAuthState} />;

  function logout() {
    clearAuth();
    setAuthState(null);
    setMessages([]);
    setView("chat");
    conversationId.current = newConversationId();
  }

  // Defense in depth alongside the role check that hides the toggle below: the
  // admin console never renders for a non-admin, even if `view` were somehow
  // left over as "admin" from a prior session (e.g. a fresh login as analyst
  // right after an admin signed out on the same tab).
  const showAdmin = view === "admin" && authState.role === "admin";

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    const q = input.trim();
    if (!q || busy) return;
    setInput("");
    setMessages((m) => [...m, { role: "user", text: q }]);
    setBusy(true);
    try {
      const data = await sendChat(q, conversationId.current);
      setMessages((m) => [...m, { role: "assistant", text: data.answer, data }]);
    } catch (err) {
      if (err instanceof AuthExpiredError) {
        logout();
        return;
      }
      setMessages((m) => [...m, { role: "assistant", text: `Request failed: ${err}`, error: true }]);
    } finally {
      setBusy(false);
      bottom.current?.scrollIntoView({ behavior: "smooth" });
    }
  }

  async function giveFeedback(i: number, rating: "up" | "down") {
    const m = messages[i];
    if (!m.data?.request_id || m.feedback || pendingFeedback.current.has(i)) return;
    pendingFeedback.current.add(i);
    setMessages((ms) => ms.map((msg, idx) => (idx === i ? { ...msg, feedbackPending: true } : msg)));
    const comment = rating === "down"
      ? window.prompt("What was wrong? (optional)") ?? undefined
      : undefined;
    try {
      await sendFeedback(m.data.request_id, conversationId.current, rating, comment);
      setMessages((ms) => ms.map((msg, idx) =>
        idx === i ? { ...msg, feedback: rating, feedbackPending: false } : msg));
    } catch {
      /* feedback is best-effort; clear the pending flag so a retry is possible */
      setMessages((ms) => ms.map((msg, idx) =>
        idx === i ? { ...msg, feedbackPending: false } : msg));
    } finally {
      pendingFeedback.current.delete(i);
    }
  }

  return (
    <div className="shell">
      <header>
        <h1>Analytics Copilot</h1>
        <span className="sub">Ask about machines, centers, utilization, service tickets</span>
        <div className="header-right">
          {authState.role === "admin" && (
            <div className="tabs">
              <button
                className={view === "chat" ? "tab active" : "tab"}
                onClick={() => setView("chat")}
              >
                Chat
              </button>
              <button
                className={view === "admin" ? "tab active" : "tab"}
                onClick={() => setView("admin")}
              >
                Admin
              </button>
            </div>
          )}
          <span className={`badge ${authState.role}`}>{authState.role}</span>
          <button className="linklike" onClick={logout}>
            sign out
          </button>
        </div>
      </header>
      {showAdmin ? <Admin /> : (
        <>
          <main>
            {messages.length === 0 && (
              <div className="hint">
                Try: <em>Which 5 centers had the most downtime hours last quarter?</em>
              </div>
            )}
            {messages.map((m, i) => (
              <div key={i} className={`msg ${m.role} ${m.data?.error_type || m.error ? "err" : ""}`}>
                <p>{m.text}</p>
                {m.data?.sql && (
                  <details>
                    <summary>SQL</summary>
                    <pre>{m.data.sql}</pre>
                  </details>
                )}
                {m.data && m.data.rows.length > 0 && (
                  <div className="tablewrap">
                    <table>
                      <thead>
                        <tr>{m.data.columns.map((c) => <th key={c}>{c}</th>)}</tr>
                      </thead>
                      <tbody>
                        {m.data.rows.slice(0, 20).map((r, ri) => (
                          <tr key={ri}>{r.map((v, ci) => <td key={ci}>{String(v ?? "")}</td>)}</tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
                {m.data && m.data.assumptions.length > 0 && (
                  <details>
                    <summary>Assumptions</summary>
                    <ul>{m.data.assumptions.map((a, ai) => <li key={ai}>{a}</li>)}</ul>
                  </details>
                )}
                {m.data?.request_id && (
                  <div className="fb">
                    {m.feedback
                      ? <span className="fb-done">feedback: {m.feedback === "up" ? "👍" : "👎"}</span>
                      : <>
                          <button disabled={m.feedbackPending} onClick={() => giveFeedback(i, "up")}>👍</button>
                          <button disabled={m.feedbackPending} onClick={() => giveFeedback(i, "down")}>👎</button>
                        </>}
                  </div>
                )}
              </div>
            ))}
            {busy && <div className="msg assistant busy">Thinking…</div>}
            <div ref={bottom} />
          </main>
          <form onSubmit={submit}>
            <input
              value={input}
              onChange={(e) => setInput(e.target.value)}
              placeholder="Ask a question about the fleet…"
              disabled={busy}
            />
            <button disabled={busy || !input.trim()}>Send</button>
          </form>
        </>
      )}
    </div>
  );
}
