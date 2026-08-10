"""Fire the demo questions at a running API and print a scorecard."""
import json
import urllib.request

QUESTIONS = [
    "Which 5 treatment centers had the most machine downtime hours in the last 90 days?",
    "How many machines do we have per model?",
    "What is the average MTTR for critical tickets by region?",
    "Which machine model had the worst delivery percentage in Q2 2026?",
    "How many open critical tickets are there right now?",
]

for q in QUESTIONS:
    body = json.dumps({"question": q}).encode()
    # NOTE: /api/chat requires a Bearer token since auth landed (this script predates
    # it and was never updated). Fixing that needs a login call plumbed through with a
    # real demo password (gen_demo_users.py never writes the plaintext anywhere), so
    # this script currently gets 401, not a working demo run -- fix the path here, but
    # treat this file as broken until that plumbing is added.
    req = urllib.request.Request(
        "http://localhost:8000/api/chat", body, {"content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            data = json.load(r)
        status = "OK " if not data.get("error_type") else f"ERR({data['error_type']})"
        print(f"[{status}] {q}\n   -> {data['answer'][:140]}\n")
    except Exception as e:  # noqa: BLE001
        print(f"[FAIL] {q} -> {e}\n")
