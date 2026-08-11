import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import App from "./App";
import { setAuth } from "./auth";

// Same fake-JWT builder App.test.tsx defines (see that file's comment): the
// frontend only ever base64url-decodes the payload client-side, no signature
// verification, so a garbage signature segment is fine for tests. Not exported
// from App.test.tsx, so duplicated here rather than importing a test file.
function fakeJwt(claims: Record<string, unknown> = {}): string {
  const b64url = (obj: unknown) =>
    btoa(JSON.stringify(obj)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  const exp = Math.floor(Date.now() / 1000) + 3600;
  return `${b64url({ alg: "HS256", typ: "JWT" })}.${b64url({ exp, ...claims })}.sig`;
}

function jsonResponse(status: number, body: unknown) {
  return { ok: status >= 200 && status < 300, status, json: async () => body } as Response;
}

const overview = {
  total_requests: 42,
  error_rate: 0.25,
  p50_latency_ms: 850,
  feedback_up: 5,
  feedback_down: 2,
  by_intent: { data_query: 30, help: 12 },
};

const requestRows = [{
  request_id: "r1", created_at: "2026-08-10T12:00:00Z", user_role: "analyst",
  intent: "data_query", status: "ok", error_type: null, e2e_ms: 1234,
  sql_text: "SELECT model FROM GOLD.DIM_MACHINE", question: "which models?",
}];

const feedbackRows = [{
  feedback_id: "f1", created_at: "2026-08-10T12:00:05Z", conversation_id: "c1",
  request_id: "r1", rating: "up", comment: "great",
}];

beforeEach(() => {
  localStorage.clear();
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

test("admin sees the console tab, analyst does not", async () => {
  setAuth({ token: fakeJwt(), role: "analyst", email: "analyst@demo" });
  render(<App />);
  expect(screen.queryByRole("button", { name: /admin/i })).toBeNull();
});

test("admin sees the console tab", async () => {
  setAuth({ token: fakeJwt(), role: "admin", email: "admin@demo" });
  render(<App />);
  expect(screen.getByRole("button", { name: /admin/i })).toBeDefined();
  // The chat form is still the default view -- the toggle exists, but it did
  // not switch anything just by rendering.
  expect(screen.getByPlaceholderText(/Ask a question/)).toBeDefined();
});

test("the console renders overview numbers from a mocked fetch", async () => {
  setAuth({ token: fakeJwt(), role: "admin", email: "admin@demo" });
  const fetchMock = vi.fn()
    .mockResolvedValueOnce(jsonResponse(200, overview))
    .mockResolvedValueOnce(jsonResponse(200, requestRows))
    .mockResolvedValueOnce(jsonResponse(200, feedbackRows));
  vi.stubGlobal("fetch", fetchMock);

  render(<App />);
  fireEvent.click(screen.getByRole("button", { name: /admin/i }));

  await waitFor(() => expect(screen.getByText("42")).toBeDefined());
  expect(screen.getByText("25.0%")).toBeDefined();
  expect(screen.getByText("850ms")).toBeDefined();
  expect(screen.getByText(/5👍 \/ 2👎/)).toBeDefined();

  // Fetched the three admin endpoints, authenticated, under /api.
  expect(fetchMock).toHaveBeenCalledTimes(3);
  const paths = fetchMock.mock.calls.map(([url]) => String(url));
  expect(paths.some((p) => p.includes("/api/admin/overview"))).toBe(true);
  expect(paths.some((p) => p.includes("/api/admin/requests"))).toBe(true);
  expect(paths.some((p) => p.includes("/api/admin/feedback"))).toBe(true);
  for (const [, options] of fetchMock.mock.calls) {
    expect(options.headers.authorization).toMatch(/^Bearer /);
  }

  // Recent-requests table and feedback list render actual row data.
  expect(screen.getByText(/SELECT model FROM GOLD.DIM_MACHINE/)).toBeDefined();
  expect(screen.getByText("great")).toBeDefined();
});

test("switching back to Chat restores the chat view", async () => {
  setAuth({ token: fakeJwt(), role: "admin", email: "admin@demo" });
  const fetchMock = vi.fn()
    .mockResolvedValueOnce(jsonResponse(200, overview))
    .mockResolvedValueOnce(jsonResponse(200, requestRows))
    .mockResolvedValueOnce(jsonResponse(200, feedbackRows));
  vi.stubGlobal("fetch", fetchMock);

  render(<App />);
  fireEvent.click(screen.getByRole("button", { name: /admin/i }));
  await waitFor(() => expect(screen.getByText("42")).toBeDefined());

  fireEvent.click(screen.getByRole("button", { name: "Chat" }));
  expect(screen.getByPlaceholderText(/Ask a question/)).toBeDefined();
});

test("a fetch failure shows a plain error line, not a crash", async () => {
  setAuth({ token: fakeJwt(), role: "admin", email: "admin@demo" });
  const fetchMock = vi.fn().mockRejectedValue(new TypeError("Failed to fetch"));
  vi.stubGlobal("fetch", fetchMock);

  render(<App />);
  fireEvent.click(screen.getByRole("button", { name: /admin/i }));

  expect(await screen.findByText(/Couldn't load admin data/)).toBeDefined();
});

// Final review, Minor finding M4: a 401 loading admin data previously rendered
// as "Couldn't load admin data: AuthExpiredError: session expired" body text,
// the same as any other fetch failure -- unlike App.tsx's own chat path, which
// already treats a 401 as a session event and logs out. An admin whose token
// expired mid-session saw a dead console instead of the login screen every
// other 401 already sends them to.
test("a 401 while loading admin data logs the user out, not a page-text error", async () => {
  setAuth({ token: fakeJwt(), role: "admin", email: "admin@demo" });
  const fetchMock = vi.fn().mockResolvedValueOnce(jsonResponse(401, { detail: "invalid token" }));
  vi.stubGlobal("fetch", fetchMock);

  render(<App />);
  fireEvent.click(screen.getByRole("button", { name: /admin/i }));

  await waitFor(() => expect(screen.getByPlaceholderText("password")).toBeDefined());
  expect(localStorage.getItem("copilot_auth")).toBeNull();
  expect(screen.queryByText(/Couldn't load admin data/)).toBeNull();
});
