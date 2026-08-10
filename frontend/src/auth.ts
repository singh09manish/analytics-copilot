export interface AuthState {
  token: string;
  role: "analyst" | "admin";
  email: string;
}

const KEY = "copilot_auth";

export function getAuth(): AuthState | null {
  const raw = localStorage.getItem(KEY);
  if (!raw) return null;
  try {
    return JSON.parse(raw) as AuthState;
  } catch {
    return null;
  }
}

export function setAuth(a: AuthState): void {
  localStorage.setItem(KEY, JSON.stringify(a));
}

export function clearAuth(): void {
  localStorage.removeItem(KEY);
}
