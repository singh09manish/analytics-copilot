import { beforeEach, expect, test } from "vitest";
import { getAuth, setAuth } from "./auth";

// Minimal unsigned JWT builder for tests: getAuth only ever base64url-decodes
// the payload segment client-side (no signature verification), so a garbage
// signature segment is fine here.
function fakeJwt(claims: Record<string, unknown>): string {
  const b64url = (obj: unknown) =>
    btoa(JSON.stringify(obj)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  return `${b64url({ alg: "HS256", typ: "JWT" })}.${b64url(claims)}.sig`;
}

beforeEach(() => {
  localStorage.clear();
});

test("getAuth returns the stored state when the token is not yet expired", () => {
  const token = fakeJwt({ sub: "analyst@demo", role: "analyst", exp: Math.floor(Date.now() / 1000) + 3600 });
  setAuth({ token, role: "analyst", email: "analyst@demo" });
  expect(getAuth()?.email).toBe("analyst@demo");
});

test("getAuth clears and returns null when the token's exp has passed", () => {
  const token = fakeJwt({ sub: "analyst@demo", role: "analyst", exp: Math.floor(Date.now() / 1000) - 3600 });
  setAuth({ token, role: "analyst", email: "analyst@demo" });
  expect(getAuth()).toBeNull();
  expect(localStorage.getItem("copilot_auth")).toBeNull();
});

test("getAuth clears and returns null for a malformed (non-JWT) token", () => {
  setAuth({ token: "not-a-jwt", role: "analyst", email: "analyst@demo" });
  expect(getAuth()).toBeNull();
  expect(localStorage.getItem("copilot_auth")).toBeNull();
});
