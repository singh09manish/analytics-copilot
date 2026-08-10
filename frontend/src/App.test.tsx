import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import App from "./App";
import { setAuth } from "./auth";

// Minimal unsigned JWT builder: the frontend only ever base64url-decodes the
// payload client-side (no signature verification, see auth.ts#isExpired), so
// a garbage signature segment is fine for tests.
function fakeJwt(claims: Record<string, unknown> = {}): string {
  const b64url = (obj: unknown) =>
    btoa(JSON.stringify(obj)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  const exp = Math.floor(Date.now() / 1000) + 3600;
  return `${b64url({ alg: "HS256", typ: "JWT" })}.${b64url({ exp, ...claims })}.sig`;
}

function jsonResponse(status: number, body: unknown) {
  return { ok: status >= 200 && status < 300, status, json: async () => body } as Response;
}

// jsdom doesn't implement scrollIntoView; App's submit() calls it unconditionally
// in a finally block, and these tests now exercise that path (unlike the
// original render-only tests), so it needs a stub or every chat-submitting
// test throws an unhandled rejection after the assertions already ran.
Element.prototype.scrollIntoView = vi.fn();

beforeEach(() => {
  localStorage.clear();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

test("renders title and input", () => {
  setAuth({ token: fakeJwt(), role: "analyst", email: "analyst@demo" });
  render(<App />);
  expect(screen.getByText("Analytics Copilot")).toBeDefined();
  expect(screen.getByPlaceholderText(/Ask a question/)).toBeDefined();
});

test("unauthenticated users see the login form", () => {
  localStorage.clear();
  render(<App />);
  expect(screen.getByPlaceholderText("password")).toBeDefined();
});

test("login success posts to /auth/login and transitions to the chat UI", async () => {
  const token = fakeJwt();
  const fetchMock = vi.fn().mockResolvedValueOnce(
    jsonResponse(200, { token, role: "analyst", email: "analyst@demo" }),
  );
  vi.stubGlobal("fetch", fetchMock);

  render(<App />);
  fireEvent.change(screen.getByPlaceholderText("password"), { target: { value: "secret" } });
  fireEvent.click(screen.getByRole("button", { name: "Sign in" }));

  await waitFor(() => expect(screen.getByPlaceholderText(/Ask a question/)).toBeDefined());

  expect(fetchMock).toHaveBeenCalledTimes(1);
  const [url, options] = fetchMock.mock.calls[0];
  expect(String(url)).toContain("/auth/login");
  expect(options.method).toBe("POST");
  expect(JSON.parse(options.body)).toEqual({ email: "analyst@demo", password: "secret" });
});

test("login failure shows an error and stays on the login screen", async () => {
  const fetchMock = vi.fn().mockResolvedValueOnce(jsonResponse(401, { detail: "invalid credentials" }));
  vi.stubGlobal("fetch", fetchMock);

  render(<App />);
  fireEvent.change(screen.getByPlaceholderText("password"), { target: { value: "wrong" } });
  fireEvent.click(screen.getByRole("button", { name: "Sign in" }));

  await waitFor(() => expect(screen.getByText("Invalid credentials")).toBeDefined());
  expect(screen.getByPlaceholderText("password")).toBeDefined();
});

test("authenticated requests attach the Authorization bearer header", async () => {
  const token = fakeJwt();
  setAuth({ token, role: "analyst", email: "analyst@demo" });
  const fetchMock = vi.fn().mockResolvedValueOnce(jsonResponse(200, {
    answer: "42 machines", sql: null, columns: [], rows: [], assumptions: [],
    error_type: null, retrieval_ms: 5, intent: "metric", request_id: "r1",
  }));
  vi.stubGlobal("fetch", fetchMock);

  render(<App />);
  fireEvent.change(screen.getByPlaceholderText(/Ask a question/), {
    target: { value: "how many machines are online" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Send" }));

  await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
  const [url, options] = fetchMock.mock.calls[0];
  expect(String(url)).toContain("/chat");
  expect(options.headers.authorization).toBe(`Bearer ${token}`);
});

test("a 401 from /chat clears auth and returns the user to the login screen", async () => {
  setAuth({ token: fakeJwt(), role: "analyst", email: "analyst@demo" });
  const fetchMock = vi.fn().mockResolvedValueOnce(jsonResponse(401, { detail: "invalid token" }));
  vi.stubGlobal("fetch", fetchMock);

  render(<App />);
  fireEvent.change(screen.getByPlaceholderText(/Ask a question/), {
    target: { value: "how many machines are online" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Send" }));

  await waitFor(() => expect(screen.getByPlaceholderText("password")).toBeDefined());
  expect(localStorage.getItem("copilot_auth")).toBeNull();
});
