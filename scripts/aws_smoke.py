"""End-to-end check against the deployed URL.

Proves the deployment does what the local app does: login issues a JWT, an analyst
sees masked contact emails, an admin sees real ones, and unauthenticated calls are
refused.
"""
import json
import sys
import time
import urllib.error
import urllib.request

# Explicit about wanting the email column specifically (not just "contact info" or
# "phone number"), so the model reliably selects a column this script can grade.
QUESTION = "List every treatment center's name along with its contact email address."

# CloudFront's /api/* cache behaviour caps origin reads at 60s (infra/cdn.tf); the ALB
# itself allows up to 180s (alb.tf). A healthy /chat call -- especially one that takes
# the agent's repair path -- can legitimately run past 60s, in which case CloudFront
# cuts it at the edge and the client never gets to use its own, longer timeout at all.
EDGE_TIMEOUT_NOTE = (
    "CloudFront's 60s edge timeout is shorter than the ALB's 180s -- this may be a "
    "slow-but-healthy response cut off at the edge, not an actual failure. Check "
    "`aws logs tail /ecs/analytics-copilot --since 15m` for whether it was still "
    "working when it got cut."
)


def post(base: str, path: str, body: dict, token: str = "", _retried: bool = False) -> tuple[int, dict]:
    req = urllib.request.Request(
        f"{base}{path}", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json",
                 **({"Authorization": f"Bearer {token}"} if token else {})},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        # 502/503/504 here is often CloudFront's edge timeout truncating a slow-but-
        # healthy backend response, not a real failure -- worth one retry before it
        # counts against the deployment.
        if e.code in (502, 503, 504) and not _retried:
            time.sleep(3)
            return post(base, path, body, token, _retried=True)
        return e.code, {}
    except urllib.error.URLError as e:
        # DNS failure, connection refused, TLS error, etc. -- report cleanly instead
        # of letting an unhandled exception crash the script with a raw traceback.
        print(f"  (connection error on {path}: {e.reason})")
        return 0, {}


def warm_up(base: str, analyst_pw: str) -> None:
    """Pay the cold-start cost (Snowflake connect + MCP subprocess spawn on the ECS
    task's first real query) here, before any of the checks that are actually
    asserted on below -- so a slow first response reads as an expected warm-up
    delay, not a spurious failure on the real checks. Best-effort and silent on
    failure: if login or the warm-up chat call doesn't work, the real checks below
    will hit the same problem and report it properly."""
    status, body = post(base, "/api/auth/login", {"email": "analyst@demo", "password": analyst_pw})
    if status != 200:
        return
    token = body.get("token", "")
    if token:
        post(base, "/api/chat", {"question": "warm up", "conversation_id": "smoke-warmup"}, token)


def main() -> None:
    if len(sys.argv) != 4:
        sys.exit("usage: aws_smoke.py <base-url> <analyst-password> <admin-password>")
    if not sys.argv[1].strip():
        sys.exit(
            "base URL is empty. Check `terraform -chdir=infra output -raw app_url` "
            "-- it should print the deployed app's URL; an empty result usually "
            "means terraform couldn't find infra/ (wrong -chdir) or the stack "
            "hasn't been applied yet."
        )
    base, analyst_pw, admin_pw = sys.argv[1].rstrip("/"), sys.argv[2], sys.argv[3]
    failures = []

    print("warming up (cold-start Snowflake connect + MCP subprocess spawn)...")
    warm_up(base, analyst_pw)

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
        if status in (502, 503, 504):
            failures.append(f"{role} chat failed: {status} ({EDGE_TIMEOUT_NOTE})")
        elif status != 200 or body.get("error_type"):
            failures.append(f"{role} chat failed: {status} {body.get('error_type')}")

    # An empty seen[role] means the response had no cell containing "@" or "MASK" --
    # i.e. the model didn't return an email column at all. That is not the same as
    # "masking verified" and must not be treated as a pass: this is the one check
    # that proves the governance story (role-based PII masking) on the live stack,
    # so silently skipping it would let SMOKE PASSED print while masking went
    # entirely unverified.
    for role in ("analyst", "admin"):
        if role not in seen:
            continue  # login or chat already failed for this role, recorded above
        if not seen[role]:
            failures.append(
                f"{role}: no email column in the response -- cannot verify masking; "
                f"the model may have chosen different columns"
            )
            continue
        if role == "analyst" and not all("MASK" in v for v in seen[role]):
            failures.append("analyst saw unmasked contact emails")
        if role == "admin" and not any("@" in v for v in seen[role]):
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
