export interface AuthState {
  token: string;
  role: "analyst" | "admin";
  email: string;
}

const KEY = "copilot_auth";

// Decodes the JWT payload segment client-side, with no signature verification.
// This is a UX check only (avoid flashing the authenticated shell for a token
// we already know is stale) -- the server's 401 response on /chat and
// /feedback is the real security backstop, enforced regardless of what this
// function returns.
function isExpired(token: string): boolean {
  try {
    const payload = token.split(".")[1];
    if (!payload) return true;
    const base64 = payload.replace(/-/g, "+").replace(/_/g, "/");
    const padded = base64.padEnd(base64.length + ((4 - (base64.length % 4)) % 4), "=");
    const claims = JSON.parse(atob(padded)) as { exp?: number };
    if (typeof claims.exp !== "number") return false;
    return claims.exp * 1000 < Date.now();
  } catch {
    return true;
  }
}

export function getAuth(): AuthState | null {
  const raw = localStorage.getItem(KEY);
  if (!raw) return null;
  let parsed: AuthState;
  try {
    parsed = JSON.parse(raw) as AuthState;
  } catch {
    return null;
  }
  if (isExpired(parsed.token)) {
    clearAuth();
    return null;
  }
  return parsed;
}

export function setAuth(a: AuthState): void {
  localStorage.setItem(KEY, JSON.stringify(a));
}

export function clearAuth(): void {
  localStorage.removeItem(KEY);
}
