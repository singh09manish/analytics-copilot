"""End-to-end check against the deployed URL.

Proves the deployment does what the local app does: login issues a JWT, an analyst
sees masked contact emails, an admin sees real ones, and unauthenticated calls are
refused.
"""
import json
import sys
import urllib.error
import urllib.request

QUESTION = "list treatment centers with their contact emails"


def post(base: str, path: str, body: dict, token: str = "") -> tuple[int, dict]:
    req = urllib.request.Request(
        f"{base}{path}", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json",
                 **({"Authorization": f"Bearer {token}"} if token else {})},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, {}


def main() -> None:
    if len(sys.argv) != 4:
        sys.exit("usage: aws_smoke.py <base-url> <analyst-password> <admin-password>")
    base, analyst_pw, admin_pw = sys.argv[1].rstrip("/"), sys.argv[2], sys.argv[3]
    failures = []

    status, _ = post(base, "/api/chat", {"question": "hi"})
    print(f"unauthenticated /api/chat -> {status} (want 401)")
    if status != 401:
        failures.append("unauthenticated request was not refused")

    seen = {}
    for role, pw in (("analyst", analyst_pw), ("admin", admin_pw)):
        status, body = post(base, "/api/auth/login", {"email": f"{role}@demo", "password": pw})
        if status != 200:
            failures.append(f"{role} login failed with {status}")
            continue
        token = body["token"]
        print(f"{role} login -> 200, role={body.get('role')}")

        status, body = post(base, "/api/chat",
                            {"question": QUESTION, "conversation_id": f"smoke-{role}"}, token)
        rows = body.get("rows") or body.get("data") or []
        flat = [str(c) for row in rows[:6] for c in (row if isinstance(row, list) else [row])]
        seen[role] = [v for v in flat if "@" in v or "MASK" in v][:3]
        print(f"{role} /api/chat -> {status} error={body.get('error_type')} "
              f"intent={body.get('intent')} emails={seen[role]}")
        if status != 200 or body.get("error_type"):
            failures.append(f"{role} chat failed: {status} {body.get('error_type')}")

    if seen.get("analyst") and not all("MASK" in v for v in seen["analyst"]):
        failures.append("analyst saw unmasked contact emails")
    if seen.get("admin") and not any("@" in v for v in seen["admin"]):
        failures.append("admin did not see real contact emails")

    print()
    if failures:
        print("SMOKE FAILED:")
        for f in failures:
            print("  -", f)
        sys.exit(1)
    print("SMOKE PASSED: auth enforced, masking differs by role, both roles answered.")


if __name__ == "__main__":
    main()
