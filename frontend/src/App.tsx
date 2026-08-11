import { useEffect, useRef, useState } from "react";
import Admin from "./Admin";
import { AuthExpiredError, sendChat, sendFeedback } from "./api";
import { clearAuth, getAuth, type AuthState } from "./auth";
import Login from "./Login";
import type { ChatResponse, Message } from "./types";
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

type Theme = "dark" | "light";

const THEME_KEY = "copilot_theme";

// The console is dark by default. A stored choice wins; otherwise we mirror the
// OS preference so the first paint matches what the CSS media query would pick.
// matchMedia is guarded because jsdom (the test environment) does not implement
// it, and localStorage can throw in a locked-down browser profile.
function initialTheme(): Theme {
  try {
    const stored = localStorage.getItem(THEME_KEY);
    if (stored === "dark" || stored === "light") return stored;
  } catch {
    /* storage unavailable -- fall through to the OS preference */
  }
  if (typeof window !== "undefined" && typeof window.matchMedia === "function"
      && window.matchMedia("(prefers-color-scheme: light)").matches) {
    return "light";
  }
  return "dark";
}

// The mask token GOVERNANCE applies to PII columns for non-admin roles (see
// warehouse/governance.sql). A masked cell is a withheld channel, not an
// error, so it gets its own deliberate amber treatment.
const MASK_TOKEN = "***MASKED***";

// Small mono readouts under an assistant answer. Every value here is real
// telemetry off the response -- a chip is omitted entirely when the backend
// did not send its value, so nothing on screen is invented.
function InstrumentStrip({ data }: { data: ChatResponse }) {
  const chips: Array<[string, string]> = [];
  if (data.intent) chips.push(["intent", data.intent]);
  if (data.retrieval_mode) chips.push(["retrieval", data.retrieval_mode]);
  if (typeof data.retrieval_ms === "number") chips.push(["retrieval ms", String(data.retrieval_ms)]);
  if (typeof data.tokens_in === "number" || typeof data.tokens_out === "number") {
    chips.push(["tokens", String((data.tokens_in ?? 0) + (data.tokens_out ?? 0))]);
  }
  if (chips.length === 0) return null;
  return (
    <ul className="strip">
      {chips.map(([k, v]) => (
        <li className="chip" key={k}>
          <span className="chip-k">{k}</span>
          <span className="chip-v">{v}</span>
        </li>
      ))}
    </ul>
  );
}

export default function App() {
  const [authState, setAuthState] = useState<AuthState | null>(getAuth());
  // Chat is always the default view -- the toggle itself only renders for admins
  // (see the rail below), so an analyst never sees or reaches "admin" here.
  const [view, setView] = useState<"chat" | "admin">("chat");
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [theme, setTheme] = useState<Theme>(initialTheme);
  const [elapsed, setElapsed] = useState(0);
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

  useEffect(() => {
    document.documentElement.setAttribute("data-theme", theme);
    try {
      localStorage.setItem(THEME_KEY, theme);
    } catch {
      /* storage unavailable -- the in-memory choice still applies */
    }
  }, [theme]);

  // The busy state shows a real elapsed-milliseconds readout rather than a
  // spinner, so the number on screen is measured, not decorative.
  useEffect(() => {
    if (!busy) return;
    const started = Date.now();
    setElapsed(0);
    const id = window.setInterval(() => setElapsed(Date.now() - started), 100);
    return () => window.clearInterval(id);
  }, [busy]);

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

  const sessionId = conversationId.current ?? "";

  return (
    <div className="shell">
      <aside className="rail">
        <div className="rail-brand">
          <span className="legend rail-eyebrow">Fleet Console</span>
          <h1 className="wordmark">Analytics Copilot</h1>
        </div>

        {authState.role === "admin" && (
          <nav className="nav" aria-label="Views">
            <button
              className={view === "chat" ? "tab active" : "tab"}
              aria-current={view === "chat" ? "page" : undefined}
              onClick={() => setView("chat")}
            >
              Chat
            </button>
            <button
              className={view === "admin" ? "tab active" : "tab"}
              aria-current={view === "admin" ? "page" : undefined}
              onClick={() => setView("admin")}
            >
              Admin
            </button>
          </nav>
        )}

        <div className="rail-meta">
          <div className="meta-row">
            <span className="legend">Access</span>
            {/* `badge-${role}`, not `badge ${role}`: a bare "admin" class here
                collides with the admin console's own .admin block and inherits
                its panel padding, blowing the pill up into a box. */}
            <span className={`badge badge-${authState.role}`}>{authState.role}</span>
          </div>
          <div className="meta-row">
            <span className="legend">Operator</span>
            <span className="meta-val">{authState.email}</span>
          </div>
          <div className="meta-row">
            <span className="legend">Session</span>
            <span className="meta-val" title={sessionId}>{sessionId.slice(0, 8)}</span>
          </div>
        </div>

        <div className="rail-foot">
          <button
            className="ghost"
            onClick={() => setTheme(theme === "dark" ? "light" : "dark")}
            aria-label={`Switch to ${theme === "dark" ? "light" : "dark"} theme`}
          >
            <svg viewBox="0 0 16 16" aria-hidden="true" fill="none" stroke="currentColor" strokeWidth="1.4">
              {theme === "dark"
                ? <><circle cx="8" cy="8" r="3.2" /><path d="M8 1v1.6M8 13.4V15M15 8h-1.6M2.6 8H1M12.9 3.1l-1.1 1.1M4.2 11.8l-1.1 1.1M12.9 12.9l-1.1-1.1M4.2 4.2L3.1 3.1" strokeLinecap="round" /></>
                : <path d="M13.4 9.6A5.8 5.8 0 0 1 6.4 2.6a5.8 5.8 0 1 0 7 7Z" strokeLinejoin="round" />}
            </svg>
            {theme === "dark" ? "Light" : "Dark"}
          </button>
          <button className="linklike" onClick={logout}>
            sign out
          </button>
        </div>
      </aside>

      {showAdmin ? <Admin onAuthExpired={logout} /> : (
        <div className="workspace">
          <main aria-live="polite" aria-busy={busy}>
            <div className="column">
              {messages.length === 0 && (
                <div className="hint">
                  <span className="legend">Standby</span>
                  <p className="hint-lead">Ask about machines, centers, utilization, service tickets.</p>
                  <p className="hint-try">
                    Try: <em>Which 5 centers had the most downtime hours last quarter?</em>
                  </p>
                </div>
              )}
              {messages.map((m, i) => (
                <div key={i} className={`msg ${m.role} ${m.data?.error_type || m.error ? "err" : ""}`}>
                  {m.role === "user" && <span className="legend">You</span>}
                  <p>{m.text}</p>
                  {m.data && <InstrumentStrip data={m.data} />}
                  {m.data?.sql && (
                    <section className="sqlpanel">
                      <span className="legend">Generated SQL</span>
                      <pre><code>{m.data.sql}</code></pre>
                    </section>
                  )}
                  {m.data && m.data.rows.length > 0 && (
                    <div className="tablewrap">
                      <table>
                        <thead>
                          <tr>{m.data.columns.map((c) => <th key={c} scope="col">{c}</th>)}</tr>
                        </thead>
                        <tbody>
                          {m.data.rows.slice(0, 20).map((r, ri) => (
                            <tr key={ri}>{r.map((v, ci) => {
                              const cell = String(v ?? "");
                              return cell === MASK_TOKEN
                                ? <td key={ci} className="masked" title="Withheld by column-level policy">{cell}</td>
                                : <td key={ci}>{cell}</td>;
                            })}</tr>
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
              {busy && (
                <div className="msg assistant busy">
                  <span className="legend">Working</span>
                  <span>
                    querying warehouse
                    <span className="caret" aria-hidden="true">▋</span>
                  </span>
                  <span className="busy-ms" aria-hidden="true">{elapsed} ms</span>
                </div>
              )}
              <div ref={bottom} />
            </div>
          </main>
          <div className="composer-dock">
            <form onSubmit={submit} className="composer">
              <label className="visually-hidden" htmlFor="question">
                Ask a question about the fleet
              </label>
              <input
                id="question"
                value={input}
                onChange={(e) => setInput(e.target.value)}
                placeholder="Ask a question about the fleet…"
                disabled={busy}
                autoComplete="off"
              />
              <button className="send" disabled={busy || !input.trim()}>Send</button>
            </form>
          </div>
        </div>
      )}
    </div>
  );
}
