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
    req = urllib.request.Request(
        "http://localhost:8000/chat", body, {"content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            data = json.load(r)
        status = "OK " if not data.get("error_type") else f"ERR({data['error_type']})"
        print(f"[{status}] {q}\n   -> {data['answer'][:140]}\n")
    except Exception as e:  # noqa: BLE001
        print(f"[FAIL] {q} -> {e}\n")
