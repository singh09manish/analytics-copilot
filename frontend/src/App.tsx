import { useRef, useState } from "react";
import { AuthExpiredError, sendChat } from "./api";
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

  if (!authState) return <Login onLogin={setAuthState} />;

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    const q = input.trim();
    if (!q || busy) return;
    setInput("");
    setMessages((m) => [...m, { role: "user", text: q }]);
    setBusy(true);
    try {
      const data = await sendChat(q, null);
      setMessages((m) => [...m, { role: "assistant", text: data.answer, data }]);
    } catch (err) {
      if (err instanceof AuthExpiredError) {
        setAuthState(null);
        setMessages([]);
        return;
      }
      setMessages((m) => [...m, { role: "assistant", text: `Request failed: ${err}` }]);
    } finally {
      setBusy(false);
      bottom.current?.scrollIntoView({ behavior: "smooth" });
    }
  }

  return (
    <div className="shell">
      <header>
        <h1>Analytics Copilot</h1>
        <span className="sub">Ask about machines, centers, utilization, service tickets</span>
        <div className="header-right">
          <span className={`badge ${authState.role}`}>{authState.role}</span>
          <button className="linklike" onClick={() => { clearAuth(); setAuthState(null); setMessages([]); }}>
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
          <div key={i} className={`msg ${m.role} ${m.data?.error_type ? "err" : ""}`}>
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
