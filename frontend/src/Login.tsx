import { useState } from "react";
import { ApiError, login } from "./api";
import { setAuth, type AuthState } from "./auth";

export default function Login({ onLogin }: { onLogin: (a: AuthState) => void }) {
  const [email, setEmail] = useState("analyst@demo");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      const res = await login(email, password);
      const a: AuthState = { token: res.token, role: res.role, email: res.email };
      setAuth(a);
      onLogin(a);
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        setError("Invalid credentials");
      } else {
        setError("Can't reach the server right now. Please try again.");
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="login-shell">
      <form onSubmit={submit} className="login-card">
        <h1>Analytics Copilot</h1>
        <p className="sub">Sign in to query the warehouse</p>
        <input value={email} onChange={(e) => setEmail(e.target.value)}
               placeholder="email" autoComplete="username" />
        <input type="password" value={password}
               onChange={(e) => setPassword(e.target.value)} placeholder="password"
               autoComplete="current-password" />
        {error && <div className="login-error">{error}</div>}
        <button disabled={busy || !password}>Sign in</button>
        <p className="hint-small">analyst@demo sees masked PII · admin@demo sees all</p>
      </form>
    </div>
  );
}
