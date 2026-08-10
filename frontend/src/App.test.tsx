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
  vi.restoreAllMocks();
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

test("the same conversation id is sent on every turn within a session", async () => {
  setAuth({ token: fakeJwt(), role: "analyst", email: "analyst@demo" });
  const answer = (requestId: string) => ({
    answer: "ok", sql: null, columns: [], rows: [], assumptions: [],
    error_type: null, retrieval_ms: 1, intent: "metric", request_id: requestId,
  });
  const fetchMock = vi.fn()
    .mockResolvedValueOnce(jsonResponse(200, answer("r1")))
    .mockResolvedValueOnce(jsonResponse(200, answer("r2")));
  vi.stubGlobal("fetch", fetchMock);

  render(<App />);
  const input = screen.getByPlaceholderText(/Ask a question/);

  fireEvent.change(input, { target: { value: "question one" } });
  fireEvent.click(screen.getByRole("button", { name: "Send" }));
  await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
  await waitFor(() => expect(screen.getByText("ok")).toBeDefined());

  fireEvent.change(input, { target: { value: "question two" } });
  fireEvent.click(screen.getByRole("button", { name: "Send" }));
  await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));

  const body1 = JSON.parse(fetchMock.mock.calls[0][1].body);
  const body2 = JSON.parse(fetchMock.mock.calls[1][1].body);
  expect(body1.conversation_id).toBeTruthy();
  expect(body1.conversation_id).not.toContain(":");
  expect(body2.conversation_id).toBe(body1.conversation_id);
});

test("thumbs-up feedback posts to /feedback once and shows the recorded state", async () => {
  setAuth({ token: fakeJwt(), role: "analyst", email: "analyst@demo" });
  const chat = {
    answer: "42 machines", sql: null, columns: [], rows: [], assumptions: [],
    error_type: null, retrieval_ms: 5, intent: "metric", request_id: "r1",
  };
  const fetchMock = vi.fn()
    .mockResolvedValueOnce(jsonResponse(200, chat))
    .mockResolvedValueOnce(jsonResponse(200, { status: "recorded" }));
  vi.stubGlobal("fetch", fetchMock);

  render(<App />);
  fireEvent.change(screen.getByPlaceholderText(/Ask a question/), {
    target: { value: "how many machines are online" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Send" }));
  await waitFor(() => expect(screen.getByText("42 machines")).toBeDefined());

  fireEvent.click(screen.getByRole("button", { name: "👍" }));
  await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));

  const [url, options] = fetchMock.mock.calls[1];
  expect(String(url)).toContain("/feedback");
  const body = JSON.parse(options.body);
  expect(body).toMatchObject({ request_id: "r1", rating: "up" });

  await waitFor(() => expect(screen.getByText("feedback: 👍")).toBeDefined());
  // Re-clicking is a no-op: the buttons are gone, so a second POST never fires.
  expect(screen.queryByRole("button", { name: "👍" })).toBeNull();
  expect(fetchMock).toHaveBeenCalledTimes(2);
});

test("assumptions render under the SQL panel when present", async () => {
  setAuth({ token: fakeJwt(), role: "analyst", email: "analyst@demo" });
  const chat = {
    answer: "12 centers", sql: "select 1", columns: [], rows: [],
    assumptions: ["Assumed last full quarter = Q2 2026"],
    error_type: null, retrieval_ms: 5, intent: "metric", request_id: "r1",
  };
  const fetchMock = vi.fn().mockResolvedValueOnce(jsonResponse(200, chat));
  vi.stubGlobal("fetch", fetchMock);

  render(<App />);
  fireEvent.change(screen.getByPlaceholderText(/Ask a question/), {
    target: { value: "how many centers" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Send" }));

  await waitFor(() => expect(screen.getByText("Assumptions")).toBeDefined());
  expect(screen.getByText("Assumed last full quarter = Q2 2026")).toBeDefined();
});

test("a network failure renders an error-styled message", async () => {
  setAuth({ token: fakeJwt(), role: "analyst", email: "analyst@demo" });
  const fetchMock = vi.fn().mockRejectedValueOnce(new TypeError("Failed to fetch"));
  vi.stubGlobal("fetch", fetchMock);

  render(<App />);
  fireEvent.change(screen.getByPlaceholderText(/Ask a question/), {
    target: { value: "how many machines are online" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Send" }));

  const errText = await screen.findByText(/Request failed/);
  expect(errText.closest(".msg")?.className).toContain("err");
});

test("two fast clicks on the same feedback button send exactly one /feedback POST", async () => {
  setAuth({ token: fakeJwt(), role: "analyst", email: "analyst@demo" });
  const chat = {
    answer: "42 machines", sql: null, columns: [], rows: [], assumptions: [],
    error_type: null, retrieval_ms: 5, intent: "metric", request_id: "r1",
  };
  // The /feedback call is deliberately left unresolved until we say so, so
  // the second click lands while the first request is still in flight --
  // this is the exact race the pending-flag guard exists to close.
  let resolveFeedback!: (res: Response) => void;
  const feedbackPromise = new Promise<Response>((resolve) => { resolveFeedback = resolve; });
  const fetchMock = vi.fn()
    .mockResolvedValueOnce(jsonResponse(200, chat))
    .mockImplementationOnce(() => feedbackPromise);
  vi.stubGlobal("fetch", fetchMock);

  render(<App />);
  fireEvent.change(screen.getByPlaceholderText(/Ask a question/), {
    target: { value: "how many machines are online" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Send" }));
  await waitFor(() => expect(screen.getByText("42 machines")).toBeDefined());

  const upButton = screen.getByRole("button", { name: "👍" });
  fireEvent.click(upButton);
  fireEvent.click(upButton);

  // Only the chat POST and a single feedback POST should have fired -- the
  // second click must not have reached sendFeedback while the first was
  // still pending.
  expect(fetchMock).toHaveBeenCalledTimes(2);

  resolveFeedback(jsonResponse(200, { status: "recorded" }));
  await waitFor(() => expect(screen.getByText("feedback: 👍")).toBeDefined());
  expect(fetchMock).toHaveBeenCalledTimes(2);
});

test("thumbs-down feedback prompts for a comment and includes it in the POST", async () => {
  setAuth({ token: fakeJwt(), role: "analyst", email: "analyst@demo" });
  const chat = {
    answer: "42 machines", sql: null, columns: [], rows: [], assumptions: [],
    error_type: null, retrieval_ms: 5, intent: "metric", request_id: "r1",
  };
  const fetchMock = vi.fn()
    .mockResolvedValueOnce(jsonResponse(200, chat))
    .mockResolvedValueOnce(jsonResponse(200, { status: "recorded" }));
  vi.stubGlobal("fetch", fetchMock);
  const promptSpy = vi.spyOn(window, "prompt").mockReturnValue("the number looks wrong");

  render(<App />);
  fireEvent.change(screen.getByPlaceholderText(/Ask a question/), {
    target: { value: "how many machines are online" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Send" }));
  await waitFor(() => expect(screen.getByText("42 machines")).toBeDefined());

  fireEvent.click(screen.getByRole("button", { name: "👎" }));
  await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));

  expect(promptSpy).toHaveBeenCalledTimes(1);
  const body = JSON.parse(fetchMock.mock.calls[1][1].body);
  expect(body).toMatchObject({ request_id: "r1", rating: "down", comment: "the number looks wrong" });

  await waitFor(() => expect(screen.getByText("feedback: 👎")).toBeDefined());
});

test("signing out and back in generates a new conversation id for the next session", async () => {
  const token = fakeJwt();
  setAuth({ token, role: "analyst", email: "analyst@demo" });
  const answer = (requestId: string) => ({
    answer: "ok", sql: null, columns: [], rows: [], assumptions: [],
    error_type: null, retrieval_ms: 1, intent: "metric", request_id: requestId,
  });
  const fetchMock = vi.fn()
    .mockResolvedValueOnce(jsonResponse(200, answer("r1"))) // session 1 chat
    .mockResolvedValueOnce(jsonResponse(200, { token, role: "analyst", email: "analyst@demo" })) // re-login
    .mockResolvedValueOnce(jsonResponse(200, answer("r2"))); // session 2 chat
  vi.stubGlobal("fetch", fetchMock);

  render(<App />);
  fireEvent.change(screen.getByPlaceholderText(/Ask a question/), { target: { value: "question one" } });
  fireEvent.click(screen.getByRole("button", { name: "Send" }));
  await waitFor(() => expect(screen.getByText("ok")).toBeDefined());
  const firstConvoId = JSON.parse(fetchMock.mock.calls[0][1].body).conversation_id;

  fireEvent.click(screen.getByRole("button", { name: "sign out" }));
  await waitFor(() => expect(screen.getByPlaceholderText("password")).toBeDefined());

  fireEvent.change(screen.getByPlaceholderText("password"), { target: { value: "secret" } });
  fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
  await waitFor(() => expect(screen.getByPlaceholderText(/Ask a question/)).toBeDefined());

  fireEvent.change(screen.getByPlaceholderText(/Ask a question/), { target: { value: "question two" } });
  fireEvent.click(screen.getByRole("button", { name: "Send" }));
  await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3));

  const secondConvoId = JSON.parse(fetchMock.mock.calls[2][1].body).conversation_id;
  expect(secondConvoId).toBeTruthy();
  expect(secondConvoId).not.toBe(firstConvoId);
});

test("a session expiry (401) resets the conversation id the same way as sign-out", async () => {
  const token = fakeJwt();
  setAuth({ token, role: "analyst", email: "analyst@demo" });
  const chat = {
    answer: "ok", sql: null, columns: [], rows: [], assumptions: [],
    error_type: null, retrieval_ms: 1, intent: "metric", request_id: "r2",
  };
  const fetchMock = vi.fn()
    .mockResolvedValueOnce(jsonResponse(401, { detail: "invalid token" })) // session 1 chat -> expired
    .mockResolvedValueOnce(jsonResponse(200, { token, role: "analyst", email: "analyst@demo" })) // re-login
    .mockResolvedValueOnce(jsonResponse(200, chat)); // session 2 chat
  vi.stubGlobal("fetch", fetchMock);

  render(<App />);
  fireEvent.change(screen.getByPlaceholderText(/Ask a question/), { target: { value: "question one" } });
  fireEvent.click(screen.getByRole("button", { name: "Send" }));
  await waitFor(() => expect(screen.getByPlaceholderText("password")).toBeDefined());
  const firstConvoId = JSON.parse(fetchMock.mock.calls[0][1].body).conversation_id;

  fireEvent.change(screen.getByPlaceholderText("password"), { target: { value: "secret" } });
  fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
  await waitFor(() => expect(screen.getByPlaceholderText(/Ask a question/)).toBeDefined());

  fireEvent.change(screen.getByPlaceholderText(/Ask a question/), { target: { value: "question two" } });
  fireEvent.click(screen.getByRole("button", { name: "Send" }));
  await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3));

  const secondConvoId = JSON.parse(fetchMock.mock.calls[2][1].body).conversation_id;
  expect(secondConvoId).toBeTruthy();
  expect(secondConvoId).not.toBe(firstConvoId);
});

test("falls back to a manually built UUID when crypto.randomUUID is unavailable", async () => {
  setAuth({ token: fakeJwt(), role: "analyst", email: "analyst@demo" });
  // Simulate a non-secure-context host: crypto.randomUUID is undefined there,
  // but crypto.getRandomValues remains available.
  vi.stubGlobal("crypto", { getRandomValues: crypto.getRandomValues.bind(crypto) });

  const chat = {
    answer: "ok", sql: null, columns: [], rows: [], assumptions: [],
    error_type: null, retrieval_ms: 1, intent: "metric", request_id: "r1",
  };
  const fetchMock = vi.fn().mockResolvedValueOnce(jsonResponse(200, chat));
  vi.stubGlobal("fetch", fetchMock);

  expect(() => render(<App />)).not.toThrow();
  fireEvent.change(screen.getByPlaceholderText(/Ask a question/), { target: { value: "q" } });
  fireEvent.click(screen.getByRole("button", { name: "Send" }));
  await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));

  const body = JSON.parse(fetchMock.mock.calls[0][1].body);
  expect(body.conversation_id).toMatch(
    /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i,
  );
});
