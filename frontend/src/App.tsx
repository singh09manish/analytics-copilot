import { useRef, useState } from "react";
import { AuthExpiredError, sendChat, sendFeedback } from "./api";
import { clearAuth, getAuth, type AuthState } from "./auth";
import Login from "./Login";
import type { Message } from "./types";
import "./App.css";

export default function App() {
  const [authState, setAuthState] = useState<AuthState | null>(getAuth());
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const bottom = useRef<HTMLDivElement>(null);
  const conversationId = useRef<string>(crypto.randomUUID());

  if (!authState) return <Login onLogin={setAuthState} />;

  function logout() {
    clearAuth();
    setAuthState(null);
    setMessages([]);
    conversationId.current = crypto.randomUUID();
  }

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
    if (!m.data?.request_id || m.feedback) return;
    const comment = rating === "down"
      ? window.prompt("What was wrong? (optional)") ?? undefined
      : undefined;
    try {
      await sendFeedback(m.data.request_id, conversationId.current, rating, comment);
      setMessages((ms) => ms.map((msg, idx) =>
        idx === i ? { ...msg, feedback: rating } : msg));
    } catch {
      /* feedback is best-effort */
    }
  }

  return (
    <div className="shell">
      <header>
        <h1>Analytics Copilot</h1>
        <span className="sub">Ask about machines, centers, utilization, service tickets</span>
        <div className="header-right">
          <span className={`badge ${authState.role}`}>{authState.role}</span>
          <button className="linklike" onClick={logout}>
            sign out
          </button>
        </div>
      </header>
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
                      <button onClick={() => giveFeedback(i, "up")}>👍</button>
                      <button onClick={() => giveFeedback(i, "down")}>👎</button>
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
    </div>
  );
}
