# Analytics Copilot — Phase 1 (Foundation) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A locally demoable vertical slice — natural-language question → Claude-generated SQL → governed Snowflake medallion warehouse → summarized answer — with seed data, dbt models, governance, vector retrieval, FastAPI, and a minimal React chat.

**Architecture:** Monorepo. Python backend (`backend/`) calls Claude via a provider abstraction, guards SQL with sqlglot, executes against Snowflake gold schema as a read-only service user (key-pair auth). Warehouse built as bronze (raw CSV loads) → silver/gold (dbt). AI Data Library (glossary + schema cards with Cortex vector embeddings) feeds retrieval. React SPA (`frontend/`) is a thin chat client.

**Tech Stack:** Python 3.12 + uv, FastAPI, anthropic SDK (model `claude-sonnet-5`), sqlglot, snowflake-connector-python, dbt-snowflake, pydantic v2, pytest, ruff · Vite + React + TypeScript, vitest · Snowflake (trial, AWS) · GitHub Actions (lint+test only in this phase).

**Phasing note:** This is Plan 1 of 4. Phase 2 (LangGraph graph, MCP server, auth/RBAC, UI polish), Phase 3 (Terraform/AWS, CI/CD deploy, evals, Admin Console), and Phase 4 (interview prep material) get their own plan files, written when their phase starts. Nothing in this plan may depend on those phases.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-08-08-analytics-copilot-design.md` — this plan implements its "Sat Aug 8" + "Sun Aug 9" scope.
- Snowflake object names exactly: database `MEDTECH_ANALYTICS`; schemas `BRONZE`, `SILVER`, `GOLD`, `COPILOT`; warehouse `COPILOT_WH`; roles `COPILOT_APP_RO`, `COPILOT_APP_WRITER`, `COPILOT_ADMIN`; service user `COPILOT_SVC`.
- LLM model id: `claude-sonnet-5` (env-overridable, never hardcoded at call sites).
- SQL guard: single SELECT (CTEs/UNION allowed), only schemas GOLD/COPILOT, auto-`LIMIT 1000`, no DDL/DML — enforced via sqlglot with dialect `snowflake`.
- All Python passes `ruff check` (line-length 100) and `pytest -m "not live"` without network/credentials. Live tests are marked `live`.
- Seed data is deterministic: `random.Random(42)` — never the global RNG.
- Secrets only via `.env` (gitignored); `.env.example` documents every variable. No secret ever committed.
- Commits: conventional prefixes (`feat:`, `test:`, `chore:`, `docs:`), each task ends in a commit.

## File Structure

```
Project root (existing git repo)
├── Makefile                          # entry points for every workflow
├── .env.example / .env (gitignored)
├── .github/workflows/ci.yml          # ruff + pytest + frontend build/test
├── backend/
│   ├── pyproject.toml                # uv-managed
│   ├── src/copilot/
│   │   ├── __init__.py
│   │   ├── config.py                 # Settings (pydantic-settings)
│   │   ├── snowflake_client.py       # connection + run_query (role-scoped)
│   │   ├── sql_guard.py              # sqlglot validation + LIMIT injection
│   │   ├── retrieval.py              # vector search over COPILOT.* (+ keyword fallback)
│   │   ├── llm/
│   │   │   ├── __init__.py
│   │   │   ├── schemas.py            # SqlDraft, QueryPlan (plan used fully in Phase 2)
│   │   │   └── provider.py           # LLMProvider protocol + AnthropicProvider
│   │   ├── agent/
│   │   │   ├── __init__.py
│   │   │   ├── prompts.py            # versioned prompt templates (PROMPT_VERSION)
│   │   │   └── pipeline.py           # linear slice: retrieve→generate→guard→execute→summarize
│   │   └── api/
│   │       ├── __init__.py
│   │       └── main.py               # FastAPI app: /healthz, /chat
│   └── tests/
│       ├── conftest.py               # fakes: FakeProvider, FakeSnowflake
│       ├── test_seed.py
│       ├── test_sql_guard.py
│       ├── test_provider.py
│       ├── test_pipeline.py
│       ├── test_api.py
│       ├── test_retrieval.py
│       └── live/test_slice_live.py   # marked live; needs .env
├── data/
│   ├── seed/generate.py              # writes data/seed/out/*.csv (out/ gitignored)
│   └── ai_library/
│       ├── glossary.yaml             # ~40 business terms
│       └── schema_cards.yaml         # per-gold-table cards
├── warehouse/
│   ├── bootstrap.sql                 # run once in Snowsight (ACCOUNTADMIN)
│   ├── governance.sql                # masking policy, tag, grants — Snowsight
│   ├── load_bronze.py                # PUT + COPY INTO as COPILOT_SVC
│   ├── load_ai_library.py            # COPILOT tables + Cortex embeddings
│   └── dbt/                          # dbt project: silver + gold models/tests
└── frontend/                         # Vite React TS chat client
    └── src/{App.tsx, api.ts, types.ts, App.css}
```

---

### Task 1: Repo scaffold, tooling, CI skeleton

**Files:**
- Create: `Makefile`, `.env.example`, `.gitignore` (extend), `backend/pyproject.toml`, `backend/src/copilot/__init__.py`, `backend/tests/__init__.py`, `.github/workflows/ci.yml`, `README.md`

**Interfaces:**
- Produces: `make lint`, `make test` (both green on empty project); uv venv in `backend/`; pytest marker `live` registered.

- [ ] **Step 1: Extend .gitignore**

Append to existing `.gitignore`:

```
.env
backend/.venv/
data/seed/out/
__pycache__/
.pytest_cache/
.ruff_cache/
node_modules/
frontend/dist/
warehouse/dbt/target/
warehouse/dbt/dbt_packages/
warehouse/dbt/logs/
secrets/
```

- [ ] **Step 2: Create backend/pyproject.toml**

```toml
[project]
name = "copilot"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
    "fastapi>=0.115",
    "uvicorn[standard]>=0.30",
    "anthropic>=0.40",
    "pydantic>=2.8",
    "pydantic-settings>=2.4",
    "snowflake-connector-python>=3.12",
    "sqlglot>=25.0",
    "cryptography>=43.0",
    "pyyaml>=6.0",
]

[dependency-groups]
dev = ["pytest>=8.0", "ruff>=0.6", "httpx>=0.27", "dbt-snowflake>=1.8"]

[tool.ruff]
line-length = 100
src = ["src", "tests"]

[tool.pytest.ini_options]
testpaths = ["tests"]
markers = ["live: requires .env credentials and network"]
addopts = "-m 'not live'"

[tool.uv]
package = true

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/copilot"]
```

- [ ] **Step 3: Create package + test skeletons**

`backend/src/copilot/__init__.py` and `backend/tests/__init__.py`: empty files.

- [ ] **Step 4: Create Makefile**

```makefile
UV := cd backend && uv

.PHONY: install lint test test-live seed api web check-env

install:
	$(UV) sync

lint:
	$(UV) run ruff check src tests

test:
	$(UV) run pytest

test-live:
	$(UV) run pytest -m live --no-header -q

seed:
	$(UV) run python ../data/seed/generate.py

load-bronze:
	$(UV) run python ../warehouse/load_bronze.py

ai-library:
	$(UV) run python ../warehouse/load_ai_library.py

dbt-run:
	$(UV) run dbt run --project-dir ../warehouse/dbt --profiles-dir ../warehouse/dbt

dbt-test:
	$(UV) run dbt test --project-dir ../warehouse/dbt --profiles-dir ../warehouse/dbt

api:
	$(UV) run uvicorn copilot.api.main:app --reload --port 8000

web:
	cd frontend && npm run dev

check-env:
	$(UV) run python ../scripts/check_env.py
```

- [ ] **Step 5: Create .env.example**

```bash
# Anthropic
ANTHROPIC_API_KEY=sk-ant-...
APP_MODEL=claude-sonnet-5

# Snowflake — account locator like ABC12345.us-east-1 (from Snowsight: Admin > Accounts)
SNOWFLAKE_ACCOUNT=
SNOWFLAKE_USER=COPILOT_SVC
SNOWFLAKE_PRIVATE_KEY_PATH=secrets/copilot_svc_key.p8
SNOWFLAKE_WAREHOUSE=COPILOT_WH
SNOWFLAKE_DATABASE=MEDTECH_ANALYTICS
SNOWFLAKE_ROLE=COPILOT_APP_RO
```

- [ ] **Step 6: Create CI workflow `.github/workflows/ci.yml`**

```yaml
name: ci
on:
  push: {branches: [main]}
  pull_request:
jobs:
  backend:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v5
      - run: cd backend && uv sync
      - run: cd backend && uv run ruff check src tests
      - run: cd backend && uv run pytest
  frontend:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with: {node-version: 22}
      - run: test -f frontend/package.json && cd frontend && npm ci && npm test -- --run && npm run build || echo "frontend not created yet"
```

- [ ] **Step 7: Create README.md**

```markdown
# Analytics Copilot

Chat assistant that answers business questions against a governed Snowflake
medallion warehouse. Claude (Anthropic API) + LangGraph + RAG over a business
glossary, with schema-checked SQL generation, an MCP tool server, RBAC, an
eval harness, and CloudWatch telemetry. Deployed on AWS via Terraform.

## Quickstart
1. `cp .env.example .env` and fill in credentials
2. `make install && make check-env`
3. Seed + warehouse: `make seed`, run `warehouse/bootstrap.sql` in Snowsight,
   `make load-bronze`, `make dbt-run dbt-test`, run `warehouse/governance.sql`,
   `make ai-library`
4. Run: `make api` and `make web`

Architecture: see `docs/superpowers/specs/2026-08-08-analytics-copilot-design.md`.
```

- [ ] **Step 8: Install + verify**

Run: `make install && make lint && make test`
Expected: sync resolves; ruff passes; pytest reports "no tests ran" (exit 5 is OK — treat as pass for this task only).

- [ ] **Step 9: Commit**

```bash
git add -A && git commit -m "chore: scaffold monorepo tooling, CI skeleton, Makefile"
```

---

### Task 2: Deterministic seed-data generator

**Files:**
- Create: `data/seed/generate.py`
- Test: `backend/tests/test_seed.py`

**Interfaces:**
- Produces: `data/seed/out/{centers.csv, machines.csv, utilization.csv, service_tickets.csv}`; importable functions `gen_centers(rng)`, `gen_machines(rng, centers)`, `gen_utilization(rng, machines)`, `gen_tickets(rng, machines)` each returning `list[dict]`; `main()` writes all four CSVs.
- Data realities later tasks rely on: 60 centers (+3 duplicate rows), 200 machines (+4 dups), utilization = one row per machine per day for 730 days ending 2026-08-07 (~146k rows, ~1% messy), ~8000 tickets. Machine models: TrueBeam, TrueBeam STx, Halcyon, Ethos, Clinac iX, ProBeam. Regions: NA, EMEA, APAC, LATAM (mixed case in raw). Emails present → masked later.

- [ ] **Step 1: Write the failing test**

`backend/tests/test_seed.py`:

```python
import importlib.util
import random
from pathlib import Path

SEED_PATH = Path(__file__).parents[2] / "data" / "seed" / "generate.py"
spec = importlib.util.spec_from_file_location("seedgen", SEED_PATH)
seedgen = importlib.util.module_from_spec(spec)
spec.loader.exec_module(seedgen)


def _all():
    rng = random.Random(42)
    centers = seedgen.gen_centers(rng)
    machines = seedgen.gen_machines(rng, centers)
    util = seedgen.gen_utilization(rng, machines)
    tickets = seedgen.gen_tickets(rng, machines)
    return centers, machines, util, tickets


def test_row_counts_and_determinism():
    c1, m1, u1, t1 = _all()
    c2, m2, u2, t2 = _all()
    assert len(c1) == 63  # 60 + 3 duplicate rows
    assert len(m1) == 204  # 200 + 4 duplicate rows
    assert len(u1) > 140_000
    assert 7000 < len(t1) < 9000
    assert c1 == c2 and m1[:50] == m2[:50] and u1[:50] == u2[:50] and t1[:50] == t2[:50]


def test_mess_is_present():
    centers, machines, util, tickets = _all()
    assert any(c["contact_email"] == "" for c in centers)
    assert len({c["center_id"] for c in centers}) == 60
    assert any("/" in m["install_date"] for m in machines)  # mixed date formats
    assert any(r["uptime_hours"] == "" for r in util)
    severities = {t["severity"] for t in tickets}
    assert "critical" in severities and "Critical" in severities  # case mess
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/test_seed.py -v`
Expected: FAIL (file not found / attribute errors).

- [ ] **Step 3: Implement `data/seed/generate.py`**

```python
"""Deterministic synthetic med-device analytics data with realistic mess."""
import csv
import random
from datetime import date, datetime, timedelta
from pathlib import Path

OUT = Path(__file__).parent / "out"
END = date(2026, 8, 7)
DAYS = 730
MODELS = ["TrueBeam", "TrueBeam STx", "Halcyon", "Ethos", "Clinac iX", "ProBeam"]
REGIONS = ["NA", "EMEA", "APAC", "LATAM"]
CITIES = {
    "NA": [("Atlanta", "USA"), ("Orlando", "USA"), ("Toronto", "Canada"), ("Chicago", "USA"),
           ("Houston", "USA"), ("Phoenix", "USA")],
    "EMEA": [("Berlin", "Germany"), ("London", "UK"), ("Madrid", "Spain"), ("Lagos", "Nigeria")],
    "APAC": [("Tokyo", "Japan"), ("Sydney", "Australia"), ("Mumbai", "India"), ("Seoul", "Korea")],
    "LATAM": [("Sao Paulo", "Brazil"), ("Mexico City", "Mexico"), ("Bogota", "Colombia")],
}
NAME_A = ["Northside", "Mercy", "St. Luke", "Riverview", "Summit", "Lakeside", "Unity",
          "Providence", "Horizon", "Beacon", "Cedar", "Evergreen"]
NAME_B = ["Oncology Center", "Cancer Institute", "Radiotherapy Clinic", "Medical Center",
          "Regional Hospital"]
DOWNTIME_REASONS = ["Beam fault", "Imaging fault", "Software error", "Cooling system",
                    "Scheduled maintenance", "MLC fault", "Power interruption"]
CATEGORIES = ["Beam Generation", "Imaging", "Software", "Cooling", "Mechanical", "Electrical"]
SEVERITIES = ["Critical", "High", "Medium", "Low"]


def gen_centers(rng: random.Random) -> list[dict]:
    rows = []
    for i in range(60):
        region = REGIONS[i % 4] if rng.random() > 0.2 else REGIONS[i % 4].lower()
        city, country = rng.choice(CITIES[REGIONS[i % 4]])
        name = f"{rng.choice(NAME_A)} {rng.choice(NAME_B)} {i:02d}"
        email = "" if rng.random() < 0.08 else f"ops{i:02d}@{name.split()[0].lower().replace('.', '')}health.org"
        rows.append({
            "center_id": f"C{i:03d}", "center_name": name, "region": region,
            "country": country, "city": city, "beds": rng.randint(40, 900),
            "contact_email": email,
            "go_live_date": (END - timedelta(days=rng.randint(400, 4000))).isoformat(),
        })
    rows += [dict(rows[i]) for i in (3, 17, 42)]  # duplicate rows (mess)
    return rows


def gen_machines(rng: random.Random, centers: list[dict]) -> list[dict]:
    ids = sorted({c["center_id"] for c in centers})
    rows = []
    for i in range(200):
        d = END - timedelta(days=rng.randint(200, 3500))
        fmt = d.isoformat() if rng.random() < 0.7 else d.strftime("%m/%d/%Y")
        rows.append({
            "machine_id": f"M{i:04d}", "center_id": rng.choice(ids),
            "model": rng.choice(MODELS), "serial": f"SN-{rng.randint(100000, 999999)}",
            "install_date": fmt, "sw_version": f"{rng.randint(15, 18)}.{rng.randint(0, 9)}",
            "status": rng.choices(["Active", "Active", "Active", "Decommissioned"])[0],
        })
    rows += [dict(rows[i]) for i in (5, 50, 111, 180)]
    return rows


def gen_utilization(rng: random.Random, machines: list[dict]) -> list[dict]:
    active = [m for m in machines[:200] if m["status"] == "Active"]
    rows = []
    for m in active:
        base = rng.uniform(0.88, 0.99)  # per-machine reliability
        for d in range(DAYS):
            day = END - timedelta(days=DAYS - 1 - d)
            planned = rng.randint(20, 42) if day.weekday() < 5 else rng.randint(0, 8)
            down = 0.0
            reason = ""
            if rng.random() > base:
                down = round(rng.uniform(0.5, 14.0), 1)
                reason = rng.choice(DOWNTIME_REASONS)
            uptime = round(24.0 - down, 1)
            lost = min(planned, int(down * 2.2))
            row = {
                "machine_id": m["machine_id"],
                "log_date": day.isoformat() if rng.random() < 0.85 else day.strftime("%m/%d/%Y"),
                "planned_fractions": planned, "delivered_fractions": max(planned - lost, 0),
                "uptime_hours": uptime, "downtime_hours": down, "downtime_reason": reason,
            }
            if rng.random() < 0.01:
                row["uptime_hours"] = ""  # mess: missing telemetry
            rows.append(row)
    return rows


def gen_tickets(rng: random.Random, machines: list[dict]) -> list[dict]:
    active = [m for m in machines[:200]]
    rows = []
    for i in range(8000):
        m = rng.choice(active)
        opened = datetime(2024, 8, 8) + timedelta(hours=rng.randint(0, DAYS * 24 - 48))
        sev = rng.choices(SEVERITIES, weights=[1, 3, 6, 5])[0]
        if rng.random() < 0.15:
            sev = sev.lower()  # mess: inconsistent case
        res_hours = round(rng.uniform(0.5, 200 if "c" in sev.lower() else 400), 1)
        closed = opened + timedelta(hours=res_hours)
        rows.append({
            "ticket_id": f"T{i:05d}", "machine_id": m["machine_id"],
            "opened_at": opened.isoformat(sep=" "),
            "closed_at": "" if rng.random() < 0.06 else closed.isoformat(sep=" "),
            "severity": sev, "category": rng.choice(CATEGORIES),
            "resolution_hours": res_hours,
            "parts_cost": round(rng.uniform(0, 25000), 2) if rng.random() < 0.5 else 0.0,
        })
    return rows


def _write(name: str, rows: list[dict]) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    with open(OUT / name, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=rows[0].keys())
        w.writeheader()
        w.writerows(rows)


def main() -> None:
    rng = random.Random(42)
    centers = gen_centers(rng)
    machines = gen_machines(rng, centers)
    _write("centers.csv", centers)
    _write("machines.csv", machines)
    _write("utilization.csv", gen_utilization(rng, machines))
    _write("service_tickets.csv", gen_tickets(rng, machines))
    print(f"wrote 4 CSVs to {OUT}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && uv run pytest tests/test_seed.py -v`
Expected: 2 PASS.

- [ ] **Step 5: Generate the files + eyeball**

Run: `make seed && head -3 data/seed/out/*.csv && wc -l data/seed/out/*.csv`
Expected: 4 CSVs; utilization ≈ 140–150k lines.

- [ ] **Step 6: Commit**

```bash
git add data/seed/generate.py backend/tests/test_seed.py
git commit -m "feat: deterministic synthetic med-device seed data with realistic mess"
```

---

### Task 3: Snowflake bootstrap (Snowsight) + service-user keypair

**Files:**
- Create: `warehouse/bootstrap.sql`, `scripts/gen_keypair.sh`, `scripts/check_env.py`

**Interfaces:**
- Produces: Snowflake account with `MEDTECH_ANALYTICS` (schemas BRONZE/SILVER/GOLD/COPILOT), warehouse `COPILOT_WH`, roles `COPILOT_APP_RO`/`COPILOT_APP_WRITER`/`COPILOT_ADMIN`, service user `COPILOT_SVC` (key-pair auth, default role `COPILOT_ADMIN` so dbt can build; app connects with explicit lesser roles), COPILOT tables `FEEDBACK`, `REQUEST_LOG`, `EVAL_RESULTS`. Private key at `secrets/copilot_svc_key.p8`.
- **USER ACTION REQUIRED:** run `scripts/gen_keypair.sh`, paste its output into bootstrap.sql placeholder, run bootstrap.sql in Snowsight as ACCOUNTADMIN, fill `.env`.

- [ ] **Step 1: Create `scripts/gen_keypair.sh`**

```bash
#!/usr/bin/env bash
set -euo pipefail
mkdir -p secrets
openssl genrsa 2048 | openssl pkcs8 -topk8 -inform PEM -out secrets/copilot_svc_key.p8 -nocrypt
openssl rsa -in secrets/copilot_svc_key.p8 -pubout -out secrets/copilot_svc_key.pub
echo "--- paste this value into bootstrap.sql RSA_PUBLIC_KEY ---"
grep -v "PUBLIC KEY" secrets/copilot_svc_key.pub | tr -d '\n'; echo
```

Run: `chmod +x scripts/gen_keypair.sh && ./scripts/gen_keypair.sh`

- [ ] **Step 2: Create `warehouse/bootstrap.sql`**

```sql
-- Run in Snowsight as ACCOUNTADMIN. One-time setup.
USE ROLE ACCOUNTADMIN;

CREATE WAREHOUSE IF NOT EXISTS COPILOT_WH
  WAREHOUSE_SIZE = XSMALL AUTO_SUSPEND = 60 AUTO_RESUME = TRUE INITIALLY_SUSPENDED = TRUE;

CREATE DATABASE IF NOT EXISTS MEDTECH_ANALYTICS;
CREATE SCHEMA IF NOT EXISTS MEDTECH_ANALYTICS.BRONZE;
CREATE SCHEMA IF NOT EXISTS MEDTECH_ANALYTICS.SILVER;
CREATE SCHEMA IF NOT EXISTS MEDTECH_ANALYTICS.GOLD;
CREATE SCHEMA IF NOT EXISTS MEDTECH_ANALYTICS.COPILOT;

CREATE ROLE IF NOT EXISTS COPILOT_ADMIN;    -- build + unmasked read
CREATE ROLE IF NOT EXISTS COPILOT_APP_RO;   -- copilot runtime reads
CREATE ROLE IF NOT EXISTS COPILOT_APP_WRITER; -- feedback/log/eval writes
GRANT ROLE COPILOT_APP_RO TO ROLE COPILOT_ADMIN;
GRANT ROLE COPILOT_APP_WRITER TO ROLE COPILOT_ADMIN;
GRANT ROLE COPILOT_ADMIN TO ROLE SYSADMIN;

GRANT USAGE ON WAREHOUSE COPILOT_WH TO ROLE COPILOT_APP_RO;
GRANT USAGE ON WAREHOUSE COPILOT_WH TO ROLE COPILOT_APP_WRITER;
GRANT USAGE ON WAREHOUSE COPILOT_WH TO ROLE COPILOT_ADMIN;
GRANT ALL ON DATABASE MEDTECH_ANALYTICS TO ROLE COPILOT_ADMIN;
GRANT ALL ON ALL SCHEMAS IN DATABASE MEDTECH_ANALYTICS TO ROLE COPILOT_ADMIN;

-- Read-only surface for the app: GOLD + COPILOT only (no bronze/silver)
GRANT USAGE ON DATABASE MEDTECH_ANALYTICS TO ROLE COPILOT_APP_RO;
GRANT USAGE ON SCHEMA MEDTECH_ANALYTICS.GOLD TO ROLE COPILOT_APP_RO;
GRANT USAGE ON SCHEMA MEDTECH_ANALYTICS.COPILOT TO ROLE COPILOT_APP_RO;
GRANT SELECT ON ALL TABLES IN SCHEMA MEDTECH_ANALYTICS.GOLD TO ROLE COPILOT_APP_RO;
GRANT SELECT ON FUTURE TABLES IN SCHEMA MEDTECH_ANALYTICS.GOLD TO ROLE COPILOT_APP_RO;
GRANT SELECT ON ALL VIEWS IN SCHEMA MEDTECH_ANALYTICS.GOLD TO ROLE COPILOT_APP_RO;
GRANT SELECT ON FUTURE VIEWS IN SCHEMA MEDTECH_ANALYTICS.GOLD TO ROLE COPILOT_APP_RO;
GRANT SELECT ON ALL TABLES IN SCHEMA MEDTECH_ANALYTICS.COPILOT TO ROLE COPILOT_APP_RO;
GRANT SELECT ON FUTURE TABLES IN SCHEMA MEDTECH_ANALYTICS.COPILOT TO ROLE COPILOT_APP_RO;

-- Writer: append-only operational tables
GRANT USAGE ON DATABASE MEDTECH_ANALYTICS TO ROLE COPILOT_APP_WRITER;
GRANT USAGE ON SCHEMA MEDTECH_ANALYTICS.COPILOT TO ROLE COPILOT_APP_WRITER;

-- Operational tables (exist before dbt so grants above cover them)
CREATE TABLE IF NOT EXISTS MEDTECH_ANALYTICS.COPILOT.FEEDBACK (
  feedback_id VARCHAR DEFAULT UUID_STRING(), conversation_id VARCHAR, request_id VARCHAR,
  rating VARCHAR, comment VARCHAR, prompt_version VARCHAR, created_at TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP()
);
CREATE TABLE IF NOT EXISTS MEDTECH_ANALYTICS.COPILOT.REQUEST_LOG (
  request_id VARCHAR, conversation_id VARCHAR, user_role VARCHAR, question VARCHAR,
  intent VARCHAR, sql_text VARCHAR, status VARCHAR, error_type VARCHAR,
  e2e_ms NUMBER, retrieval_ms NUMBER, tokens_in NUMBER, tokens_out NUMBER,
  prompt_version VARCHAR, created_at TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP()
);
CREATE TABLE IF NOT EXISTS MEDTECH_ANALYTICS.COPILOT.EVAL_RESULTS (
  run_id VARCHAR, case_id VARCHAR, kind VARCHAR, passed BOOLEAN, score FLOAT,
  detail VARCHAR, git_sha VARCHAR, prompt_version VARCHAR, created_at TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP()
);
GRANT INSERT ON TABLE MEDTECH_ANALYTICS.COPILOT.FEEDBACK TO ROLE COPILOT_APP_WRITER;
GRANT INSERT ON TABLE MEDTECH_ANALYTICS.COPILOT.REQUEST_LOG TO ROLE COPILOT_APP_WRITER;
GRANT INSERT ON TABLE MEDTECH_ANALYTICS.COPILOT.EVAL_RESULTS TO ROLE COPILOT_APP_WRITER;

-- Service user: key-pair auth only (no password), TYPE=SERVICE avoids MFA policy
CREATE USER IF NOT EXISTS COPILOT_SVC
  TYPE = SERVICE
  DEFAULT_ROLE = COPILOT_ADMIN
  DEFAULT_WAREHOUSE = COPILOT_WH
  RSA_PUBLIC_KEY = 'PASTE_OUTPUT_OF_gen_keypair_HERE';
GRANT ROLE COPILOT_ADMIN TO USER COPILOT_SVC;
GRANT ROLE COPILOT_APP_RO TO USER COPILOT_SVC;
GRANT ROLE COPILOT_APP_WRITER TO USER COPILOT_SVC;

-- Bronze landing: internal stage + CSV format
CREATE FILE FORMAT IF NOT EXISTS MEDTECH_ANALYTICS.BRONZE.CSV_FMT
  TYPE = CSV SKIP_HEADER = 1 FIELD_OPTIONALLY_ENCLOSED_BY = '"' EMPTY_FIELD_AS_NULL = TRUE;
CREATE STAGE IF NOT EXISTS MEDTECH_ANALYTICS.BRONZE.SEED_STAGE FILE_FORMAT = MEDTECH_ANALYTICS.BRONZE.CSV_FMT;
```

- [ ] **Step 3: Create `scripts/check_env.py`**

```python
"""Verify all three credentials work before building anything."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "backend" / "src"))


def main() -> int:
    ok = True
    from dotenv import main as _  # noqa: F401 — not used; we read env directly

    for var in ("ANTHROPIC_API_KEY", "SNOWFLAKE_ACCOUNT", "SNOWFLAKE_USER"):
        if not os.environ.get(var):
            print(f"MISSING env: {var}")
            ok = False
    try:
        import anthropic

        client = anthropic.Anthropic()
        r = client.messages.create(
            model=os.environ.get("APP_MODEL", "claude-sonnet-5"),
            max_tokens=16, messages=[{"role": "user", "content": "say ok"}],
        )
        print(f"anthropic OK: {r.content[0].text[:20]!r}")
    except Exception as e:  # noqa: BLE001
        print(f"anthropic FAIL: {e}")
        ok = False
    try:
        from copilot.snowflake_client import SnowflakeClient

        sf = SnowflakeClient(role="COPILOT_ADMIN")
        cols, rows = sf.run_query("SELECT CURRENT_ROLE(), CURRENT_ACCOUNT()")
        print(f"snowflake OK: {rows[0]}")
    except Exception as e:  # noqa: BLE001
        print(f"snowflake FAIL: {e}")
        ok = False
    print("ALL GOOD" if ok else "FIX THE ABOVE FIRST")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
```

Note: `check_env.py` imports `copilot.snowflake_client` which is written in Task 5. Running `make check-env` before Task 5 will report snowflake FAIL — that's expected; the anthropic check still runs. `python-dotenv` ships with pydantic-settings' dependencies; if the import fails, delete that line (env comes from the Makefile exporting `.env` — see note below). Add to the top of the Makefile:

```makefile
ifneq (,$(wildcard .env))
include .env
export
endif
```

- [ ] **Step 4: USER ACTION — run bootstrap**

1. `./scripts/gen_keypair.sh` → copy the printed key line.
2. Paste into `bootstrap.sql` replacing `PASTE_OUTPUT_OF_gen_keypair_HERE` **in Snowsight only** (do not commit the key into the repo file).
3. Snowsight → Worksheets → paste the whole file → Run All as ACCOUNTADMIN.
4. `cp .env.example .env`, fill `SNOWFLAKE_ACCOUNT` (Snowsight → account menu → copy account identifier, format `ORGNAME-ACCTNAME` or locator) and `ANTHROPIC_API_KEY`.

Verification (Snowsight): `SHOW SCHEMAS IN DATABASE MEDTECH_ANALYTICS;` → 4 schemas + INFORMATION_SCHEMA/PUBLIC.

- [ ] **Step 5: Commit**

```bash
git add warehouse/bootstrap.sql scripts/gen_keypair.sh scripts/check_env.py Makefile
git commit -m "feat: snowflake bootstrap (roles, service user, stages, ops tables)"
```

---

### Task 4: Config + Snowflake client

**Files:**
- Create: `backend/src/copilot/config.py`, `backend/src/copilot/snowflake_client.py`
- Test: `backend/tests/test_snowflake_client.py`

**Interfaces:**
- Produces:
  - `copilot.config.Settings` (pydantic-settings, reads `.env` in repo root and process env) with fields: `anthropic_api_key: str`, `app_model: str = "claude-sonnet-5"`, `snowflake_account: str`, `snowflake_user: str`, `snowflake_private_key_path: str`, `snowflake_warehouse: str`, `snowflake_database: str`, `snowflake_role: str`; `get_settings()` cached accessor.
  - `SnowflakeClient(role: str | None = None)` with `.run_query(sql: str, params: tuple = ()) -> tuple[list[str], list[tuple]]` (column names, rows) and `.execute_many(statements: list[str])`. Role passed at construction pins the session role.

- [ ] **Step 1: Write the failing test**

`backend/tests/test_snowflake_client.py`:

```python
from unittest.mock import MagicMock, patch

from copilot.snowflake_client import SnowflakeClient


def _fake_settings():
    s = MagicMock()
    s.snowflake_account = "acct"
    s.snowflake_user = "COPILOT_SVC"
    s.snowflake_private_key_path = "secrets/copilot_svc_key.p8"
    s.snowflake_warehouse = "COPILOT_WH"
    s.snowflake_database = "MEDTECH_ANALYTICS"
    s.snowflake_role = "COPILOT_APP_RO"
    return s


@patch("copilot.snowflake_client.get_settings")
@patch("copilot.snowflake_client.snowflake.connector.connect")
@patch("copilot.snowflake_client._load_private_key", return_value=b"DERKEY")
def test_run_query_returns_columns_and_rows(_key, connect, settings):
    settings.return_value = _fake_settings()
    cur = MagicMock()
    cur.description = [("N",), ("V",)]
    cur.fetchall.return_value = [(1, "a")]
    connect.return_value.cursor.return_value = cur
    client = SnowflakeClient(role="COPILOT_ADMIN")
    cols, rows = client.run_query("SELECT 1")
    assert cols == ["N", "V"] and rows == [(1, "a")]
    assert connect.call_args.kwargs["role"] == "COPILOT_ADMIN"
    assert connect.call_args.kwargs["private_key"] == b"DERKEY"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/test_snowflake_client.py -v`
Expected: FAIL — module doesn't exist.

- [ ] **Step 3: Implement config.py**

```python
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).parents[3]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=REPO_ROOT / ".env", extra="ignore")

    anthropic_api_key: str = ""
    app_model: str = "claude-sonnet-5"
    snowflake_account: str = ""
    snowflake_user: str = "COPILOT_SVC"
    snowflake_private_key_path: str = "secrets/copilot_svc_key.p8"
    snowflake_warehouse: str = "COPILOT_WH"
    snowflake_database: str = "MEDTECH_ANALYTICS"
    snowflake_role: str = "COPILOT_APP_RO"


@lru_cache
def get_settings() -> Settings:
    return Settings()
```

- [ ] **Step 4: Implement snowflake_client.py**

```python
from pathlib import Path

import snowflake.connector
from cryptography.hazmat.primitives import serialization

from copilot.config import REPO_ROOT, get_settings


def _load_private_key(path: str) -> bytes:
    p = Path(path)
    if not p.is_absolute():
        p = REPO_ROOT / p
    key = serialization.load_pem_private_key(p.read_bytes(), password=None)
    return key.private_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


class SnowflakeClient:
    """Thin connection wrapper. One instance per role; connects lazily, reconnects if dropped."""

    def __init__(self, role: str | None = None):
        self._settings = get_settings()
        self._role = role or self._settings.snowflake_role
        self._conn: snowflake.connector.SnowflakeConnection | None = None

    def _connection(self) -> snowflake.connector.SnowflakeConnection:
        if self._conn is None or self._conn.is_closed():
            s = self._settings
            self._conn = snowflake.connector.connect(
                account=s.snowflake_account,
                user=s.snowflake_user,
                private_key=_load_private_key(s.snowflake_private_key_path),
                warehouse=s.snowflake_warehouse,
                database=s.snowflake_database,
                role=self._role,
                client_session_keep_alive=False,
            )
        return self._conn

    def run_query(self, sql: str, params: tuple = ()) -> tuple[list[str], list[tuple]]:
        cur = self._connection().cursor()
        try:
            cur.execute(sql, params or None)
            cols = [d[0] for d in cur.description] if cur.description else []
            return cols, cur.fetchall()
        finally:
            cur.close()

    def execute_many(self, statements: list[str]) -> None:
        for stmt in statements:
            self.run_query(stmt)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd backend && uv run pytest tests/test_snowflake_client.py -v`
Expected: PASS.

- [ ] **Step 6: Live check (needs Task 3 user action done)**

Run: `make check-env`
Expected: `anthropic OK`, `snowflake OK: ('COPILOT_ADMIN', ...)`, `ALL GOOD`.

- [ ] **Step 7: Commit**

```bash
git add backend/src/copilot/config.py backend/src/copilot/snowflake_client.py backend/tests/test_snowflake_client.py
git commit -m "feat: settings + role-scoped snowflake client (key-pair auth)"
```

---

### Task 5: Bronze load

**Files:**
- Create: `warehouse/load_bronze.py`

**Interfaces:**
- Consumes: `SnowflakeClient` (Task 4), seed CSVs (Task 2), stage/format (Task 3).
- Produces: BRONZE tables `RAW_CENTERS`, `RAW_MACHINES`, `RAW_UTILIZATION`, `RAW_SERVICE_TICKETS` — all columns VARCHAR + `_LOADED_AT`, full reload each run (idempotent).

- [ ] **Step 1: Implement `warehouse/load_bronze.py`**

```python
"""Land seed CSVs into BRONZE as-is (all VARCHAR). Idempotent full reload."""
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "backend" / "src"))
from copilot.snowflake_client import SnowflakeClient  # noqa: E402

SEED = Path(__file__).parents[1] / "data" / "seed" / "out"
TABLES = {
    "RAW_CENTERS": "centers.csv",
    "RAW_MACHINES": "machines.csv",
    "RAW_UTILIZATION": "utilization.csv",
    "RAW_SERVICE_TICKETS": "service_tickets.csv",
}


def main() -> None:
    sf = SnowflakeClient(role="COPILOT_ADMIN")
    for table, fname in TABLES.items():
        path = SEED / fname
        with open(path) as f:
            headers = next(csv.reader(f))
        cols = ", ".join(f"{h.upper()} VARCHAR" for h in headers)
        fq = f"MEDTECH_ANALYTICS.BRONZE.{table}"
        sf.execute_many([
            f"CREATE OR REPLACE TABLE {fq} ({cols}, _LOADED_AT TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP())",
            f"PUT file://{path.resolve()} @MEDTECH_ANALYTICS.BRONZE.SEED_STAGE/{table}/ OVERWRITE=TRUE AUTO_COMPRESS=TRUE",
        ])
        col_list = ", ".join(h.upper() for h in headers)
        sel = ", ".join(f"${i + 1}" for i in range(len(headers)))
        sf.run_query(
            f"COPY INTO {fq} ({col_list}) FROM (SELECT {sel} FROM "
            f"@MEDTECH_ANALYTICS.BRONZE.SEED_STAGE/{table}/) "
            f"FILE_FORMAT=(FORMAT_NAME='MEDTECH_ANALYTICS.BRONZE.CSV_FMT') PURGE=TRUE"
        )
        _, n = sf.run_query(f"SELECT COUNT(*) FROM {fq}")
        print(f"{table}: {n[0][0]} rows")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run + verify counts**

Run: `make load-bronze`
Expected: `RAW_CENTERS: 63`, `RAW_MACHINES: 204`, `RAW_UTILIZATION: ~146000`, `RAW_SERVICE_TICKETS: 8000`.

- [ ] **Step 3: Commit**

```bash
git add warehouse/load_bronze.py
git commit -m "feat: bronze loader (PUT + COPY INTO, idempotent)"
```

---

### Task 6: dbt project — silver + gold + tests

**Files:**
- Create: `warehouse/dbt/dbt_project.yml`, `warehouse/dbt/profiles.yml`, `warehouse/dbt/models/sources.yml`, `warehouse/dbt/models/silver/{centers.sql, machines.sql, machine_utilization_daily.sql, service_tickets.sql, schema.yml}`, `warehouse/dbt/models/gold/{dim_treatment_center.sql, dim_machine.sql, dim_date.sql, fact_machine_utilization.sql, fact_service_ticket.sql, v_center_monthly_kpis.sql, schema.yml}`

**Interfaces:**
- Consumes: BRONZE tables (Task 5).
- Produces: SILVER tables (CENTERS, MACHINES, MACHINE_UTILIZATION_DAILY, SERVICE_TICKETS) and GOLD tables/views (DIM_TREATMENT_CENTER, DIM_MACHINE, DIM_DATE, FACT_MACHINE_UTILIZATION, FACT_SERVICE_TICKET, V_CENTER_MONTHLY_KPIS). Gold column names below are the contract for schema cards (Task 10) and all later SQL generation.

- [ ] **Step 1: dbt_project.yml**

```yaml
name: medtech_warehouse
version: "1.0"
profile: medtech
model-paths: ["models"]
models:
  medtech_warehouse:
    silver:
      +materialized: table
      +schema: SILVER
    gold:
      +materialized: table
      +schema: GOLD
```

- [ ] **Step 2: profiles.yml (env-var driven, safe to commit)**

```yaml
medtech:
  target: dev
  outputs:
    dev:
      type: snowflake
      account: "{{ env_var('SNOWFLAKE_ACCOUNT') }}"
      user: "{{ env_var('SNOWFLAKE_USER') }}"
      private_key_path: "{{ env_var('SNOWFLAKE_PRIVATE_KEY_PATH') }}"
      role: COPILOT_ADMIN
      database: MEDTECH_ANALYTICS
      warehouse: COPILOT_WH
      schema: PUBLIC
      threads: 4
```

Note: dbt generates schemas as `PUBLIC_SILVER` unless overridden — add `warehouse/dbt/macros/generate_schema_name.sql`:

```sql
{% macro generate_schema_name(custom_schema_name, node) -%}
    {%- if custom_schema_name is none -%}{{ target.schema }}{%- else -%}{{ custom_schema_name | trim }}{%- endif -%}
{%- endmacro %}
```

- [ ] **Step 3: models/sources.yml**

```yaml
version: 2
sources:
  - name: bronze
    database: MEDTECH_ANALYTICS
    schema: BRONZE
    tables: [{name: RAW_CENTERS}, {name: RAW_MACHINES}, {name: RAW_UTILIZATION}, {name: RAW_SERVICE_TICKETS}]
```

- [ ] **Step 4: silver models**

`models/silver/centers.sql`:

```sql
select
    center_id,
    center_name,
    upper(region) as region,
    country,
    city,
    try_to_number(beds) as beds,
    nullif(contact_email, '') as contact_email,
    try_to_date(go_live_date) as go_live_date
from {{ source('bronze', 'RAW_CENTERS') }}
qualify row_number() over (partition by center_id order by _loaded_at desc) = 1
```

`models/silver/machines.sql`:

```sql
select
    machine_id,
    center_id,
    model,
    serial,
    coalesce(try_to_date(install_date), try_to_date(install_date, 'MM/DD/YYYY')) as install_date,
    sw_version,
    initcap(status) as status
from {{ source('bronze', 'RAW_MACHINES') }}
qualify row_number() over (partition by machine_id order by _loaded_at desc) = 1
```

`models/silver/machine_utilization_daily.sql`:

```sql
select
    machine_id,
    coalesce(try_to_date(log_date), try_to_date(log_date, 'MM/DD/YYYY')) as log_date,
    try_to_number(planned_fractions) as planned_fractions,
    try_to_number(delivered_fractions) as delivered_fractions,
    try_to_decimal(uptime_hours, 5, 1) as uptime_hours,
    try_to_decimal(downtime_hours, 5, 1) as downtime_hours,
    nullif(downtime_reason, '') as downtime_reason
from {{ source('bronze', 'RAW_UTILIZATION') }}
qualify row_number() over (partition by machine_id, log_date order by _loaded_at desc) = 1
```

`models/silver/service_tickets.sql`:

```sql
select
    ticket_id,
    machine_id,
    try_to_timestamp_ntz(opened_at) as opened_at,
    try_to_timestamp_ntz(nullif(closed_at, '')) as closed_at,
    initcap(severity) as severity,
    category,
    try_to_decimal(resolution_hours, 8, 1) as resolution_hours,
    try_to_decimal(parts_cost, 12, 2) as parts_cost
from {{ source('bronze', 'RAW_SERVICE_TICKETS') }}
qualify row_number() over (partition by ticket_id order by _loaded_at desc) = 1
```

`models/silver/schema.yml`:

```yaml
version: 2
models:
  - name: centers
    columns:
      - name: center_id
        tests: [unique, not_null]
      - name: region
        tests:
          - accepted_values: {values: [NA, EMEA, APAC, LATAM]}
  - name: machines
    columns:
      - name: machine_id
        tests: [unique, not_null]
      - name: install_date
        tests: [not_null]
  - name: machine_utilization_daily
    columns:
      - name: log_date
        tests: [not_null]
  - name: service_tickets
    columns:
      - name: ticket_id
        tests: [unique, not_null]
      - name: severity
        tests:
          - accepted_values: {values: [Critical, High, Medium, Low]}
```

- [ ] **Step 5: gold models**

`models/gold/dim_treatment_center.sql`:

```sql
select center_id, center_name, region, country, city, beds, contact_email, go_live_date
from {{ ref('centers') }}
```

`models/gold/dim_machine.sql`:

```sql
select m.machine_id, m.center_id, m.model, m.serial, m.install_date, m.sw_version, m.status
from {{ ref('machines') }} m
```

`models/gold/dim_date.sql`:

```sql
with spine as (
    select dateadd(day, seq4(), '2024-01-01'::date) as date_day
    from table(generator(rowcount => 1100))
)
select
    date_day,
    year(date_day) as year,
    quarter(date_day) as quarter,
    month(date_day) as month,
    monthname(date_day) as month_name,
    dayofweek(date_day) as day_of_week,
    case when dayofweek(date_day) in (0, 6) then true else false end as is_weekend
from spine
where date_day <= '2026-12-31'
```

`models/gold/fact_machine_utilization.sql`:

```sql
{{ config(cluster_by=['log_date', 'machine_id']) }}
select
    u.log_date,
    u.machine_id,
    m.center_id,
    u.planned_fractions,
    u.delivered_fractions,
    u.uptime_hours,
    u.downtime_hours,
    u.downtime_reason
from {{ ref('machine_utilization_daily') }} u
join {{ ref('machines') }} m using (machine_id)
where u.log_date is not null
```

`models/gold/fact_service_ticket.sql`:

```sql
select
    t.ticket_id,
    t.machine_id,
    m.center_id,
    t.opened_at,
    t.closed_at,
    t.severity,
    t.category,
    t.resolution_hours,
    t.parts_cost,
    (t.closed_at is null) as is_open
from {{ ref('service_tickets') }} t
join {{ ref('machines') }} m using (machine_id)
```

`models/gold/v_center_monthly_kpis.sql`:

```sql
{{ config(materialized='view') }}
select
    date_trunc('month', f.log_date)::date as month,
    c.center_id,
    c.center_name,
    c.region,
    sum(f.planned_fractions) as planned_fractions,
    sum(f.delivered_fractions) as delivered_fractions,
    round(sum(f.delivered_fractions) / nullif(sum(f.planned_fractions), 0) * 100, 1) as delivery_pct,
    sum(f.downtime_hours) as total_downtime_hours,
    round(sum(f.downtime_hours) / nullif(sum(f.uptime_hours + f.downtime_hours), 0) * 100, 2) as downtime_pct
from {{ ref('fact_machine_utilization') }} f
join {{ ref('dim_treatment_center') }} c using (center_id)
group by 1, 2, 3, 4
```

`models/gold/schema.yml`:

```yaml
version: 2
models:
  - name: dim_treatment_center
    columns:
      - name: center_id
        tests: [unique, not_null]
  - name: dim_machine
    columns:
      - name: machine_id
        tests: [unique, not_null]
      - name: center_id
        tests:
          - relationships: {to: ref('dim_treatment_center'), field: center_id}
  - name: fact_machine_utilization
    columns:
      - name: machine_id
        tests:
          - relationships: {to: ref('dim_machine'), field: machine_id}
  - name: fact_service_ticket
    columns:
      - name: ticket_id
        tests: [unique, not_null]
```

- [ ] **Step 6: Run dbt + tests**

Run: `make dbt-run && make dbt-test`
Expected: all models build; all tests pass. If `accepted_values` on severity fails, check silver `initcap` handled the case mess — fix the model, not the test.

- [ ] **Step 7: Sanity queries (Snowsight or via client)**

```sql
SELECT COUNT(*) FROM MEDTECH_ANALYTICS.GOLD.FACT_MACHINE_UTILIZATION;  -- ~140k (dedup + null dates dropped)
SELECT region, COUNT(*) FROM MEDTECH_ANALYTICS.GOLD.DIM_TREATMENT_CENTER GROUP BY 1;  -- 4 regions, 60 total
```

- [ ] **Step 8: Commit**

```bash
git add warehouse/dbt
git commit -m "feat: dbt medallion models (silver clean + gold star schema) with tests"
```

---

### Task 7: Governance — masking policy + verification

**Files:**
- Create: `warehouse/governance.sql`, `scripts/verify_governance.py`

**Interfaces:**
- Consumes: GOLD tables (Task 6), roles (Task 3).
- Produces: masked `contact_email` for `COPILOT_APP_RO`, clear for `COPILOT_ADMIN`. Run **after every full dbt rebuild** of DIM_TREATMENT_CENTER (dbt `CREATE OR REPLACE` drops the policy binding).

- [ ] **Step 1: Create `warehouse/governance.sql`**

```sql
-- Run in Snowsight as ACCOUNTADMIN after dbt builds GOLD. Re-run after full-refresh rebuilds.
USE ROLE ACCOUNTADMIN;
USE DATABASE MEDTECH_ANALYTICS;
USE SCHEMA GOLD;

CREATE TAG IF NOT EXISTS PII_TAG ALLOWED_VALUES 'email', 'phone', 'name';

CREATE MASKING POLICY IF NOT EXISTS MASK_EMAIL AS (val STRING) RETURNS STRING ->
  CASE WHEN CURRENT_ROLE() IN ('ACCOUNTADMIN', 'COPILOT_ADMIN') THEN val
       ELSE '***MASKED***' END;

ALTER TABLE DIM_TREATMENT_CENTER MODIFY COLUMN CONTACT_EMAIL SET MASKING POLICY MASK_EMAIL;
ALTER TABLE DIM_TREATMENT_CENTER MODIFY COLUMN CONTACT_EMAIL SET TAG PII_TAG = 'email';
```

- [ ] **Step 2: Create `scripts/verify_governance.py`**

```python
"""Same query, two roles — proves masking + RBAC. Also proves RO cannot see SILVER."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "backend" / "src"))
from copilot.snowflake_client import SnowflakeClient  # noqa: E402

Q = "SELECT center_id, contact_email FROM MEDTECH_ANALYTICS.GOLD.DIM_TREATMENT_CENTER LIMIT 3"

ro = SnowflakeClient(role="COPILOT_APP_RO")
admin = SnowflakeClient(role="COPILOT_ADMIN")
print("RO   :", ro.run_query(Q)[1])
print("ADMIN:", admin.run_query(Q)[1])
try:
    ro.run_query("SELECT COUNT(*) FROM MEDTECH_ANALYTICS.SILVER.CENTERS")
    print("FAIL: RO could read SILVER")
    sys.exit(1)
except Exception:
    print("OK: RO blocked from SILVER")
masked = all(r[1] in (None, "***MASKED***") for r in ro.run_query(Q)[1])
clear = any(r[1] and "@" in r[1] for r in admin.run_query(Q)[1])
print("MASKING OK" if masked and clear else "MASKING BROKEN")
sys.exit(0 if masked and clear else 1)
```

- [ ] **Step 3: USER ACTION — run governance.sql in Snowsight, then verify**

Run: `cd backend && uv run python ../scripts/verify_governance.py`
Expected: RO rows show `***MASKED***`, ADMIN rows show real emails, `OK: RO blocked from SILVER`, `MASKING OK`.

- [ ] **Step 4: Commit**

```bash
git add warehouse/governance.sql scripts/verify_governance.py
git commit -m "feat: masking policy + PII tag + governance verification script"
```

---

### Task 8: LLM schemas + Anthropic provider

**Files:**
- Create: `backend/src/copilot/llm/__init__.py`, `backend/src/copilot/llm/schemas.py`, `backend/src/copilot/llm/provider.py`
- Test: `backend/tests/test_provider.py`

**Interfaces:**
- Produces:
  - `schemas.SqlDraft(BaseModel)`: `sql: str`, `tables_used: list[str]`, `assumptions: list[str] = []`
  - `schemas.QueryPlan(BaseModel)`: `intent: Literal["data_query", "glossary_lookup", "smalltalk", "unsupported"]`, `entities: list[str] = []` (fully used in Phase 2; defined now so the provider test covers generic schemas)
  - `provider.LLMResult(BaseModel)`: `value: Any`, `tokens_in: int`, `tokens_out: int`
  - `provider.LLMProvider` (Protocol): `structured(system: str, user: str, schema: type[BaseModel], max_tokens: int = 1500) -> LLMResult`; `text(system: str, user: str, max_tokens: int = 1000) -> LLMResult`
  - `provider.AnthropicProvider(client: anthropic.Anthropic | None = None)` implementing it; on Pydantic validation failure retries once with the error appended; SDK-level `max_retries=2` for transient faults.

- [ ] **Step 1: Write the failing tests**

`backend/tests/test_provider.py`:

```python
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from copilot.llm.provider import AnthropicProvider
from copilot.llm.schemas import SqlDraft


def _resp(tool_input):
    return SimpleNamespace(
        content=[SimpleNamespace(type="tool_use", input=tool_input)],
        usage=SimpleNamespace(input_tokens=100, output_tokens=20),
    )


def test_structured_parses_valid_output():
    client = MagicMock()
    client.messages.create.return_value = _resp(
        {"sql": "SELECT 1", "tables_used": ["GOLD.DIM_MACHINE"], "assumptions": []}
    )
    p = AnthropicProvider(client=client)
    out = p.structured(system="s", user="u", schema=SqlDraft)
    assert out.value.sql == "SELECT 1"
    assert out.tokens_in == 100
    tool = client.messages.create.call_args.kwargs["tools"][0]
    assert tool["input_schema"] == SqlDraft.model_json_schema()


def test_structured_retries_once_on_validation_error():
    client = MagicMock()
    client.messages.create.side_effect = [
        _resp({"wrong_field": True}),
        _resp({"sql": "SELECT 2", "tables_used": [], "assumptions": ["retried"]}),
    ]
    p = AnthropicProvider(client=client)
    out = p.structured(system="s", user="u", schema=SqlDraft)
    assert out.value.sql == "SELECT 2"
    assert client.messages.create.call_count == 2
    retry_user = client.messages.create.call_args.kwargs["messages"][0]["content"]
    assert "validation" in retry_user.lower()


def test_structured_raises_after_second_failure():
    client = MagicMock()
    client.messages.create.side_effect = [_resp({"bad": 1}), _resp({"bad": 2})]
    p = AnthropicProvider(client=client)
    with pytest.raises(ValueError, match="schema-invalid"):
        p.structured(system="s", user="u", schema=SqlDraft)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && uv run pytest tests/test_provider.py -v`
Expected: FAIL — modules missing.

- [ ] **Step 3: Implement schemas.py**

```python
from typing import Literal

from pydantic import BaseModel, Field


class SqlDraft(BaseModel):
    sql: str = Field(description="One Snowflake SELECT statement, fully qualified tables")
    tables_used: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)


class QueryPlan(BaseModel):
    intent: Literal["data_query", "glossary_lookup", "smalltalk", "unsupported"]
    entities: list[str] = Field(default_factory=list)
```

Create empty `backend/src/copilot/llm/__init__.py`.

- [ ] **Step 4: Implement provider.py**

```python
from typing import Any, Protocol

import anthropic
from pydantic import BaseModel, ValidationError

from copilot.config import get_settings


class LLMResult(BaseModel):
    value: Any
    tokens_in: int = 0
    tokens_out: int = 0


class LLMProvider(Protocol):
    def structured(self, system: str, user: str, schema: type[BaseModel],
                   max_tokens: int = 1500) -> LLMResult: ...

    def text(self, system: str, user: str, max_tokens: int = 1000) -> LLMResult: ...


class AnthropicProvider:
    """Claude via the Anthropic API. Forced tool-use for schema-checked structured output."""

    def __init__(self, client: anthropic.Anthropic | None = None):
        settings = get_settings()
        self._client = client or anthropic.Anthropic(
            api_key=settings.anthropic_api_key, max_retries=2
        )
        self._model = settings.app_model

    def _call_structured(self, system: str, user: str, schema: type[BaseModel],
                         max_tokens: int) -> tuple[BaseModel | None, Any]:
        resp = self._client.messages.create(
            model=self._model, max_tokens=max_tokens, system=system,
            messages=[{"role": "user", "content": user}],
            tools=[{"name": "emit", "description": f"Emit a {schema.__name__}",
                    "input_schema": schema.model_json_schema()}],
            tool_choice={"type": "tool", "name": "emit"},
        )
        block = next(b for b in resp.content if b.type == "tool_use")
        try:
            return schema.model_validate(block.input), resp
        except ValidationError as e:
            return None, (resp, e)

    def structured(self, system: str, user: str, schema: type[BaseModel],
                   max_tokens: int = 1500) -> LLMResult:
        value, ctx = self._call_structured(system, user, schema, max_tokens)
        if value is not None:
            resp = ctx
            return LLMResult(value=value, tokens_in=resp.usage.input_tokens,
                             tokens_out=resp.usage.output_tokens)
        _, err = ctx
        retry_user = (f"{user}\n\nYour previous output failed schema validation:\n{err}\n"
                      f"Emit a corrected {schema.__name__}.")
        value, ctx = self._call_structured(system, retry_user, schema, max_tokens)
        if value is None:
            raise ValueError(f"LLM output schema-invalid twice for {schema.__name__}")
        resp = ctx
        return LLMResult(value=value, tokens_in=resp.usage.input_tokens,
                         tokens_out=resp.usage.output_tokens)

    def text(self, system: str, user: str, max_tokens: int = 1000) -> LLMResult:
        resp = self._client.messages.create(
            model=self._model, max_tokens=max_tokens, system=system,
            messages=[{"role": "user", "content": user}],
        )
        out = "".join(b.text for b in resp.content if b.type == "text")
        return LLMResult(value=out, tokens_in=resp.usage.input_tokens,
                         tokens_out=resp.usage.output_tokens)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd backend && uv run pytest tests/test_provider.py -v`
Expected: 3 PASS.

- [ ] **Step 6: Commit**

```bash
git add backend/src/copilot/llm backend/tests/test_provider.py
git commit -m "feat: LLM provider abstraction with schema-checked structured output"
```

---

### Task 9: SQL guard (TDD)

**Files:**
- Create: `backend/src/copilot/sql_guard.py`
- Test: `backend/tests/test_sql_guard.py`

**Interfaces:**
- Produces: `sql_guard.SqlGuardError(Exception)` with `.reason: str`; `sql_guard.validate(sql: str) -> str` — returns normalized Snowflake SQL with `LIMIT 1000` injected when absent; raises `SqlGuardError` on any violation. Allowed schemas: GOLD, COPILOT; allowed database: MEDTECH_ANALYTICS or unqualified.

- [ ] **Step 1: Write the failing tests**

`backend/tests/test_sql_guard.py`:

```python
import pytest

from copilot.sql_guard import SqlGuardError, validate


def test_valid_select_gets_limit_injected():
    out = validate("SELECT model, COUNT(*) FROM GOLD.DIM_MACHINE GROUP BY model")
    assert "LIMIT 1000" in out


def test_existing_limit_preserved():
    out = validate("SELECT * FROM GOLD.DIM_MACHINE LIMIT 5")
    assert "LIMIT 5" in out and "1000" not in out


def test_cte_and_union_allowed():
    sql = """WITH x AS (SELECT center_id FROM GOLD.DIM_TREATMENT_CENTER)
             SELECT * FROM x UNION ALL SELECT center_id FROM GOLD.DIM_TREATMENT_CENTER"""
    assert "LIMIT" in validate(sql)


@pytest.mark.parametrize("bad", [
    "DROP TABLE GOLD.DIM_MACHINE",
    "INSERT INTO GOLD.DIM_MACHINE VALUES (1)",
    "UPDATE GOLD.DIM_MACHINE SET model='x'",
    "DELETE FROM GOLD.DIM_MACHINE",
    "CREATE TABLE GOLD.T (a INT)",
    "SELECT 1; SELECT 2",
])
def test_ddl_dml_and_multistatement_rejected(bad):
    with pytest.raises(SqlGuardError):
        validate(bad)


def test_bronze_schema_rejected():
    with pytest.raises(SqlGuardError, match="schema"):
        validate("SELECT * FROM BRONZE.RAW_CENTERS")


def test_unqualified_table_rejected():
    with pytest.raises(SqlGuardError, match="qualify"):
        validate("SELECT * FROM DIM_MACHINE")


def test_fully_qualified_ok():
    out = validate("SELECT * FROM MEDTECH_ANALYTICS.GOLD.DIM_MACHINE")
    assert "LIMIT 1000" in out


def test_wrong_database_rejected():
    with pytest.raises(SqlGuardError, match="database"):
        validate("SELECT * FROM OTHERDB.GOLD.DIM_MACHINE")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && uv run pytest tests/test_sql_guard.py -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement sql_guard.py**

```python
"""Deterministic SQL guard: layer 1 of 3 (app validator, MCP server, Snowflake role)."""
import sqlglot
from sqlglot import expressions as exp

ALLOWED_SCHEMAS = {"GOLD", "COPILOT"}
ALLOWED_DBS = {"", "MEDTECH_ANALYTICS"}
BANNED = (exp.Insert, exp.Update, exp.Delete, exp.Drop, exp.Create, exp.Alter,
          exp.Merge, exp.TruncateTable, exp.Command, exp.Grant)
DEFAULT_LIMIT = 1000


class SqlGuardError(Exception):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def validate(sql: str) -> str:
    try:
        statements = sqlglot.parse(sql, read="snowflake")
    except sqlglot.errors.ParseError as e:
        raise SqlGuardError(f"SQL failed to parse: {e}") from e
    statements = [s for s in statements if s is not None]
    if len(statements) != 1:
        raise SqlGuardError("exactly one statement is allowed")
    stmt = statements[0]
    if not isinstance(stmt, (exp.Select, exp.Union)):
        raise SqlGuardError("only SELECT statements are allowed")
    for node in stmt.walk():
        if isinstance(node, BANNED):
            raise SqlGuardError(f"{node.key.upper()} is not allowed")
    cte_names = {c.alias_or_name.upper() for c in stmt.find_all(exp.CTE)}
    for t in stmt.find_all(exp.Table):
        name = t.name.upper()
        schema = (t.db or "").upper()
        db = (t.catalog or "").upper()
        if not schema:
            if name in cte_names:
                continue
            raise SqlGuardError(f"qualify table {name} as SCHEMA.TABLE")
        if db not in ALLOWED_DBS:
            raise SqlGuardError(f"database {db} is not allowed")
        if schema not in ALLOWED_SCHEMAS:
            raise SqlGuardError(f"schema {schema} is not allowed (use GOLD)")
    if not stmt.args.get("limit"):
        stmt = stmt.limit(DEFAULT_LIMIT)
    return stmt.sql(dialect="snowflake")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && uv run pytest tests/test_sql_guard.py -v`
Expected: all PASS. If `exp.Grant` doesn't exist in the installed sqlglot version, drop it from `BANNED` (Command catches it).

- [ ] **Step 5: Commit**

```bash
git add backend/src/copilot/sql_guard.py backend/tests/test_sql_guard.py
git commit -m "feat: sqlglot SQL guard (SELECT-only, schema allowlist, auto-LIMIT)"
```

---

### Task 10: AI library content + retrieval

**Files:**
- Create: `data/ai_library/glossary.yaml`, `data/ai_library/schema_cards.yaml`, `warehouse/load_ai_library.py`, `backend/src/copilot/retrieval.py`
- Test: `backend/tests/test_retrieval.py`

**Interfaces:**
- Consumes: `SnowflakeClient` (Task 4), COPILOT schema (Task 3).
- Produces:
  - COPILOT tables `GLOSSARY(term, definition, related_tables, embedding VECTOR(FLOAT, 768))` and `SCHEMA_CARDS(table_name, card, embedding VECTOR(FLOAT, 768))`.
  - `retrieval.RetrievedContext(BaseModel)`: `schema_cards: list[str]`, `glossary: list[str]`, `retrieval_ms: int`
  - `retrieval.retrieve(question: str, sf: SnowflakeClient, k_cards: int = 3, k_terms: int = 5) -> RetrievedContext` — Cortex vector search; on any Cortex error falls back to keyword scoring (`_keyword_fallback`, pure Python over full table contents) and sets `RetrievedContext.mode = "keyword"`.

- [ ] **Step 1: Create `data/ai_library/glossary.yaml`** (abridged here to 12 entries — write all of these; Phase 4 may extend)

```yaml
- term: fraction
  definition: A single radiotherapy treatment session delivered to a patient. Daily machine
    throughput is measured in delivered fractions.
  related_tables: [GOLD.FACT_MACHINE_UTILIZATION]
- term: delivered vs planned fractions
  definition: Delivered fractions divided by planned fractions; the core throughput KPI.
    Values below 95% usually indicate downtime or scheduling gaps.
  related_tables: [GOLD.FACT_MACHINE_UTILIZATION, GOLD.V_CENTER_MONTHLY_KPIS]
- term: downtime percent
  definition: downtime_hours / (uptime_hours + downtime_hours) * 100 for a machine-day or
    aggregate. The uptime SLA target is 98%.
  related_tables: [GOLD.FACT_MACHINE_UTILIZATION]
- term: uptime SLA
  definition: Contractual availability target of 98% uptime per machine per quarter.
  related_tables: [GOLD.FACT_MACHINE_UTILIZATION]
- term: MTTR
  definition: Mean time to repair — average resolution_hours over closed service tickets.
  related_tables: [GOLD.FACT_SERVICE_TICKET]
- term: critical ticket
  definition: Service ticket with severity 'Critical'; machine is typically non-operational.
  related_tables: [GOLD.FACT_SERVICE_TICKET]
- term: installed base
  definition: The set of machines installed at treatment centers, tracked in DIM_MACHINE
    with model, install_date and status.
  related_tables: [GOLD.DIM_MACHINE]
- term: treatment center
  definition: A hospital or clinic operating one or more radiotherapy machines.
  related_tables: [GOLD.DIM_TREATMENT_CENTER]
- term: linac model
  definition: Machine product family (TrueBeam, TrueBeam STx, Halcyon, Ethos, Clinac iX,
    ProBeam) stored in DIM_MACHINE.model.
  related_tables: [GOLD.DIM_MACHINE]
- term: go-live
  definition: Date a treatment center began clinical operation (DIM_TREATMENT_CENTER.go_live_date).
  related_tables: [GOLD.DIM_TREATMENT_CENTER]
- term: region
  definition: Sales/operations region of a center - one of NA, EMEA, APAC, LATAM.
  related_tables: [GOLD.DIM_TREATMENT_CENTER]
- term: open ticket
  definition: Service ticket with no closed_at timestamp (is_open = true).
  related_tables: [GOLD.FACT_SERVICE_TICKET]
```

- [ ] **Step 2: Create `data/ai_library/schema_cards.yaml`** — one card per gold object; the `card` text is what gets embedded and what the SQL prompt sees:

```yaml
- table_name: GOLD.DIM_TREATMENT_CENTER
  card: |
    GOLD.DIM_TREATMENT_CENTER - one row per treatment center (hospital/clinic).
    Columns: center_id (PK, 'C000'), center_name, region (NA/EMEA/APAC/LATAM),
    country, city, beds (int), contact_email (PII, masked for most roles),
    go_live_date (date). Join to facts via center_id.
    Sample questions: how many centers per region; newest go-lives.
- table_name: GOLD.DIM_MACHINE
  card: |
    GOLD.DIM_MACHINE - one row per installed radiotherapy machine (linac).
    Columns: machine_id (PK, 'M0000'), center_id (FK), model (TrueBeam, TrueBeam STx,
    Halcyon, Ethos, Clinac iX, ProBeam), serial, install_date (date), sw_version,
    status (Active/Decommissioned).
    Sample questions: installed base by model/region; oldest machines.
- table_name: GOLD.DIM_DATE
  card: |
    GOLD.DIM_DATE - calendar spine 2024-01-01..2026-12-31. Columns: date_day (PK),
    year, quarter, month, month_name, day_of_week, is_weekend.
- table_name: GOLD.FACT_MACHINE_UTILIZATION
  card: |
    GOLD.FACT_MACHINE_UTILIZATION - one row per machine per day, ~140k rows,
    2024-08-08..2026-08-07. Columns: log_date, machine_id (FK), center_id (FK),
    planned_fractions, delivered_fractions, uptime_hours, downtime_hours (0 when
    no fault), downtime_reason (NULL when no downtime; values like 'Beam fault',
    'Cooling system', 'Scheduled maintenance').
    downtime percent = downtime_hours/(uptime_hours+downtime_hours)*100.
    Sample questions: downtime by center last quarter; utilization trends by model.
- table_name: GOLD.FACT_SERVICE_TICKET
  card: |
    GOLD.FACT_SERVICE_TICKET - one row per service ticket, ~8k rows. Columns:
    ticket_id (PK), machine_id (FK), center_id (FK), opened_at (timestamp),
    closed_at (NULL if open), severity (Critical/High/Medium/Low), category
    (Beam Generation/Imaging/Software/Cooling/Mechanical/Electrical),
    resolution_hours, parts_cost, is_open (boolean).
    MTTR = AVG(resolution_hours) over closed tickets.
    Sample questions: MTTR by severity; open critical tickets; parts cost by model.
- table_name: GOLD.V_CENTER_MONTHLY_KPIS
  card: |
    GOLD.V_CENTER_MONTHLY_KPIS - pre-aggregated monthly KPIs per center (view).
    Columns: month (first of month), center_id, center_name, region,
    planned_fractions, delivered_fractions, delivery_pct, total_downtime_hours,
    downtime_pct. Prefer this view for monthly/trend questions.
```

- [ ] **Step 3: Create `warehouse/load_ai_library.py`**

```python
"""Create + populate COPILOT.GLOSSARY and COPILOT.SCHEMA_CARDS with Cortex embeddings."""
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parents[1] / "backend" / "src"))
from copilot.snowflake_client import SnowflakeClient  # noqa: E402

LIB = Path(__file__).parents[1] / "data" / "ai_library"
EMBED = "SNOWFLAKE.CORTEX.EMBED_TEXT_768('snowflake-arctic-embed-m-v1.5', %s)"


def main() -> None:
    sf = SnowflakeClient(role="COPILOT_ADMIN")
    sf.execute_many([
        "CREATE OR REPLACE TABLE MEDTECH_ANALYTICS.COPILOT.GLOSSARY ("
        "term VARCHAR, definition VARCHAR, related_tables VARCHAR, embedding VECTOR(FLOAT, 768))",
        "CREATE OR REPLACE TABLE MEDTECH_ANALYTICS.COPILOT.SCHEMA_CARDS ("
        "table_name VARCHAR, card VARCHAR, embedding VECTOR(FLOAT, 768))",
    ])
    glossary = yaml.safe_load((LIB / "glossary.yaml").read_text())
    for g in glossary:
        text = f"{g['term']}: {g['definition']}"
        sf.run_query(
            "INSERT INTO MEDTECH_ANALYTICS.COPILOT.GLOSSARY "
            f"SELECT %s, %s, %s, {EMBED}",
            (g["term"], g["definition"], ",".join(g["related_tables"]), text),
        )
    cards = yaml.safe_load((LIB / "schema_cards.yaml").read_text())
    for c in cards:
        sf.run_query(
            "INSERT INTO MEDTECH_ANALYTICS.COPILOT.SCHEMA_CARDS "
            f"SELECT %s, %s, {EMBED}",
            (c["table_name"], c["card"], c["card"]),
        )
    for t in ("GLOSSARY", "SCHEMA_CARDS"):
        _, n = sf.run_query(f"SELECT COUNT(*) FROM MEDTECH_ANALYTICS.COPILOT.{t}")
        print(f"{t}: {n[0][0]} rows")


if __name__ == "__main__":
    main()
```

If Cortex is unavailable in the trial region, this script fails on the first INSERT — that is the decision point for the keyword fallback (retrieval.py handles it transparently; the tables then store NULL embeddings via a modified INSERT dropping the EMBED call: swap `{EMBED}` for `NULL`).

- [ ] **Step 4: Write the failing retrieval test (fallback path — no network)**

`backend/tests/test_retrieval.py`:

```python
from copilot.retrieval import RetrievedContext, _keyword_fallback


CARDS = [
    ("GOLD.FACT_MACHINE_UTILIZATION", "downtime hours uptime fractions machine day"),
    ("GOLD.DIM_TREATMENT_CENTER", "center region country city beds email"),
    ("GOLD.FACT_SERVICE_TICKET", "ticket severity category resolution repair"),
]
TERMS = [
    ("downtime percent", "downtime_hours over total hours"),
    ("MTTR", "mean time to repair average resolution_hours"),
]


def test_keyword_fallback_ranks_by_overlap():
    ctx = _keyword_fallback("Which centers had the most downtime?", CARDS, TERMS, 2, 1)
    assert isinstance(ctx, RetrievedContext)
    assert ctx.mode == "keyword"
    assert "FACT_MACHINE_UTILIZATION" in ctx.schema_cards[0]
    assert "downtime percent" in ctx.glossary[0]
```

- [ ] **Step 5: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/test_retrieval.py -v`
Expected: FAIL — module missing.

- [ ] **Step 6: Implement retrieval.py**

```python
import time

from pydantic import BaseModel

from copilot.snowflake_client import SnowflakeClient

EMBED = "SNOWFLAKE.CORTEX.EMBED_TEXT_768('snowflake-arctic-embed-m-v1.5', %s)"


class RetrievedContext(BaseModel):
    schema_cards: list[str]
    glossary: list[str]
    retrieval_ms: int = 0
    mode: str = "vector"


def _keyword_fallback(question: str, cards: list[tuple], terms: list[tuple],
                      k_cards: int, k_terms: int) -> RetrievedContext:
    words = {w.strip("?,.").lower() for w in question.split() if len(w) > 3}

    def score(text: str) -> int:
        return sum(1 for w in words if w in text.lower())

    ranked_cards = sorted(cards, key=lambda c: score(c[0] + " " + c[1]), reverse=True)
    ranked_terms = sorted(terms, key=lambda t: score(t[0] + " " + t[1]), reverse=True)
    return RetrievedContext(
        schema_cards=[c[1] for c in ranked_cards[:k_cards]],
        glossary=[f"{t[0]}: {t[1]}" for t in ranked_terms[:k_terms]],
        mode="keyword",
    )


def retrieve(question: str, sf: SnowflakeClient, k_cards: int = 3,
             k_terms: int = 5) -> RetrievedContext:
    start = time.monotonic()
    try:
        _, card_rows = sf.run_query(
            "SELECT card FROM MEDTECH_ANALYTICS.COPILOT.SCHEMA_CARDS "
            f"ORDER BY VECTOR_COSINE_SIMILARITY(embedding, {EMBED}) DESC LIMIT {k_cards}",
            (question,),
        )
        _, term_rows = sf.run_query(
            "SELECT term || ': ' || definition FROM MEDTECH_ANALYTICS.COPILOT.GLOSSARY "
            f"ORDER BY VECTOR_COSINE_SIMILARITY(embedding, {EMBED}) DESC LIMIT {k_terms}",
            (question,),
        )
        ctx = RetrievedContext(
            schema_cards=[r[0] for r in card_rows],
            glossary=[r[0] for r in term_rows],
        )
    except Exception:
        _, cards = sf.run_query(
            "SELECT table_name, card FROM MEDTECH_ANALYTICS.COPILOT.SCHEMA_CARDS")
        _, terms = sf.run_query(
            "SELECT term, definition FROM MEDTECH_ANALYTICS.COPILOT.GLOSSARY")
        ctx = _keyword_fallback(question, cards, terms, k_cards, k_terms)
    ctx.retrieval_ms = int((time.monotonic() - start) * 1000)
    return ctx
```

- [ ] **Step 7: Run tests, load the library, live-verify**

Run: `cd backend && uv run pytest tests/test_retrieval.py -v` → PASS.
Run: `make ai-library` → `GLOSSARY: 12 rows`, `SCHEMA_CARDS: 6 rows`.
Live check (Snowsight): `SELECT table_name FROM MEDTECH_ANALYTICS.COPILOT.SCHEMA_CARDS ORDER BY VECTOR_COSINE_SIMILARITY(embedding, SNOWFLAKE.CORTEX.EMBED_TEXT_768('snowflake-arctic-embed-m-v1.5', 'downtime last quarter')) DESC LIMIT 2;` → FACT_MACHINE_UTILIZATION first.

- [ ] **Step 8: Commit**

```bash
git add data/ai_library warehouse/load_ai_library.py backend/src/copilot/retrieval.py backend/tests/test_retrieval.py
git commit -m "feat: AI data library (glossary + schema cards) with Cortex vector retrieval"
```

---

### Task 11: Prompts + vertical-slice pipeline

**Files:**
- Create: `backend/src/copilot/agent/__init__.py`, `backend/src/copilot/agent/prompts.py`, `backend/src/copilot/agent/pipeline.py`
- Test: `backend/tests/conftest.py`, `backend/tests/test_pipeline.py`

**Interfaces:**
- Consumes: `retrieval.retrieve`, `sql_guard.validate`, `AnthropicProvider`, `SnowflakeClient`.
- Produces:
  - `prompts.PROMPT_VERSION = "v1"`; `prompts.sql_system(context: RetrievedContext) -> str`; `prompts.summarize_system() -> str`
  - `pipeline.ChatResponse(BaseModel)`: `answer: str`, `sql: str | None`, `columns: list[str]`, `rows: list[list]`, `assumptions: list[str]`, `error_type: str | None`, `retrieval_ms: int`, `tokens_in: int`, `tokens_out: int`
  - `pipeline.answer_question(question: str, provider: LLMProvider, sf: SnowflakeClient) -> ChatResponse` — never raises; failures land in `error_type` + friendly `answer`. (Phase 2 replaces the internals with LangGraph; the signature and ChatResponse stay.)

- [ ] **Step 1: Implement prompts.py**

```python
from copilot.retrieval import RetrievedContext

PROMPT_VERSION = "v1"

TODAY = "2026-08-08"  # demo data ends 2026-08-07; keeps 'last quarter' well-defined


def sql_system(context: RetrievedContext) -> str:
    cards = "\n\n".join(context.schema_cards)
    glossary = "\n".join(f"- {g}" for g in context.glossary)
    return f"""You are a senior analytics engineer writing Snowflake SQL.
Today's date is {TODAY}.

Rules:
- Emit exactly one SELECT statement for Snowflake.
- Fully qualify tables as GOLD.<TABLE> (never bronze/silver).
- Use ILIKE for text comparisons. Round percentages to 1 decimal.
- Do not add a LIMIT unless the question asks for top-N (a safety LIMIT is added downstream).
- If the question is ambiguous, choose the most business-obvious reading and record it
  in assumptions.

Available tables:
{cards}

Business glossary:
{glossary}"""


def summarize_system() -> str:
    return f"""You are an analytics copilot for a medical-device company.
Today's date is {TODAY}. Answer the user's question from the query results provided.
Cite concrete numbers. Round sensibly. If the result set is empty, say so and suggest
a plausible next question. One short paragraph, then bullet points only if there are
3+ distinct figures. Never invent data not present in the results."""
```

- [ ] **Step 2: Implement pipeline.py**

```python
import csv
import io

from pydantic import BaseModel

from copilot.agent import prompts
from copilot.llm.provider import LLMProvider
from copilot.llm.schemas import SqlDraft
from copilot.retrieval import retrieve
from copilot.snowflake_client import SnowflakeClient
from copilot.sql_guard import SqlGuardError, validate

MAX_SUMMARY_ROWS = 50


class ChatResponse(BaseModel):
    answer: str
    sql: str | None = None
    columns: list[str] = []
    rows: list[list] = []
    assumptions: list[str] = []
    error_type: str | None = None
    retrieval_ms: int = 0
    tokens_in: int = 0
    tokens_out: int = 0


def _rows_as_csv(columns: list[str], rows: list[tuple]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(columns)
    w.writerows(rows[:MAX_SUMMARY_ROWS])
    return buf.getvalue()


def answer_question(question: str, provider: LLMProvider, sf: SnowflakeClient) -> ChatResponse:
    tokens_in = tokens_out = 0
    context = retrieve(question, sf)
    try:
        draft_res = provider.structured(
            system=prompts.sql_system(context), user=question, schema=SqlDraft)
        draft: SqlDraft = draft_res.value
        tokens_in += draft_res.tokens_in
        tokens_out += draft_res.tokens_out
    except ValueError:
        return ChatResponse(
            answer="I couldn't turn that into a query. Try rephrasing with the metric "
                   "and time range you care about.",
            error_type="llm", retrieval_ms=context.retrieval_ms)
    try:
        safe_sql = validate(draft.sql)
    except SqlGuardError as e:
        return ChatResponse(
            answer=f"I generated a query the safety rules rejected ({e.reason}). "
                   "Try rephrasing your question.",
            sql=draft.sql, error_type="validation", retrieval_ms=context.retrieval_ms,
            tokens_in=tokens_in, tokens_out=tokens_out)
    try:
        columns, rows = sf.run_query(safe_sql)
    except Exception as e:  # snowflake errors -> graceful message
        return ChatResponse(
            answer="The query failed against the warehouse. This usually means I "
                   "misread the schema - try asking a bit differently.",
            sql=safe_sql, error_type="snowflake", retrieval_ms=context.retrieval_ms,
            assumptions=[str(e)[:200]], tokens_in=tokens_in, tokens_out=tokens_out)
    summary = provider.text(
        system=prompts.summarize_system(),
        user=f"Question: {question}\n\nSQL:\n{safe_sql}\n\nResults (CSV, first "
             f"{MAX_SUMMARY_ROWS} rows):\n{_rows_as_csv(columns, rows)}")
    tokens_in += summary.tokens_in
    tokens_out += summary.tokens_out
    return ChatResponse(
        answer=summary.value, sql=safe_sql, columns=columns,
        rows=[list(r) for r in rows[:200]], assumptions=draft.assumptions,
        retrieval_ms=context.retrieval_ms, tokens_in=tokens_in, tokens_out=tokens_out)
```

Create empty `backend/src/copilot/agent/__init__.py`.

- [ ] **Step 3: Write conftest fakes + pipeline tests**

`backend/tests/conftest.py`:

```python
from pydantic import BaseModel

from copilot.llm.provider import LLMResult
from copilot.llm.schemas import SqlDraft


class FakeProvider:
    """Scriptable LLMProvider double."""

    def __init__(self, sql="SELECT model FROM GOLD.DIM_MACHINE", answer="Here you go."):
        self.sql = sql
        self.answer = answer

    def structured(self, system: str, user: str, schema: type[BaseModel],
                   max_tokens: int = 1500) -> LLMResult:
        return LLMResult(value=SqlDraft(sql=self.sql, tables_used=["GOLD.DIM_MACHINE"]),
                         tokens_in=10, tokens_out=5)

    def text(self, system: str, user: str, max_tokens: int = 1000) -> LLMResult:
        return LLMResult(value=self.answer, tokens_in=10, tokens_out=5)


class FakeSnowflake:
    """Returns canned rows; records queries. Raises if .fail is set."""

    def __init__(self):
        self.queries = []
        self.fail = False
        self.result = (["MODEL"], [("TrueBeam",), ("Halcyon",)])

    def run_query(self, sql: str, params: tuple = ()):
        self.queries.append(sql)
        if self.fail:
            raise RuntimeError("SQL compilation error: invalid identifier")
        if "SCHEMA_CARDS" in sql or "GLOSSARY" in sql:
            raise RuntimeError("no cortex in test")  # forces keyword fallback path
        return self.result
```

Wait — the fallback path in `retrieve()` re-queries SCHEMA_CARDS/GLOSSARY without Cortex; FakeSnowflake must serve those. Use this FakeSnowflake instead (final version):

```python
class FakeSnowflake:
    def __init__(self):
        self.queries = []
        self.fail = False
        self.result = (["MODEL"], [("TrueBeam",), ("Halcyon",)])

    def run_query(self, sql: str, params: tuple = ()):
        self.queries.append(sql)
        if "VECTOR_COSINE_SIMILARITY" in sql:
            raise RuntimeError("no cortex in tests")
        if "SCHEMA_CARDS" in sql:
            return (["TABLE_NAME", "CARD"],
                    [("GOLD.DIM_MACHINE", "machines models installed"),
                     ("GOLD.FACT_MACHINE_UTILIZATION", "downtime uptime fractions")])
        if "GLOSSARY" in sql:
            return (["TERM", "DEFINITION"], [("MTTR", "mean repair"), ("fraction", "session")])
        if self.fail:
            raise RuntimeError("SQL compilation error: invalid identifier")
        return self.result
```

`backend/tests/test_pipeline.py`:

```python
from copilot.agent.pipeline import answer_question
from tests.conftest import FakeProvider, FakeSnowflake


def test_happy_path_returns_answer_sql_rows():
    r = answer_question("Which models do we have?", FakeProvider(), FakeSnowflake())
    assert r.error_type is None
    assert r.answer == "Here you go."
    assert "LIMIT 1000" in r.sql
    assert r.rows == [["TrueBeam"], ["Halcyon"]]
    assert r.tokens_in == 20


def test_guard_rejection_is_graceful():
    r = answer_question("drop it", FakeProvider(sql="DROP TABLE GOLD.DIM_MACHINE"),
                        FakeSnowflake())
    assert r.error_type == "validation"
    assert "safety rules" in r.answer


def test_snowflake_error_is_graceful():
    sf = FakeSnowflake()
    sf.fail = True
    r = answer_question("Which models?", FakeProvider(), sf)
    assert r.error_type == "snowflake"
    assert r.sql is not None
```

Note: `from tests.conftest import ...` requires `backend/tests/__init__.py` to exist (Task 1 created it) — if imports still fail, move the fakes into a `backend/tests/fakes.py` module and import from there; conftest.py is not importable in some pytest configs.

- [ ] **Step 4: Run tests**

Run: `cd backend && uv run pytest tests/test_pipeline.py -v`
Expected: 3 PASS.

- [ ] **Step 5: Live smoke test**

`backend/tests/live/test_slice_live.py` (+ empty `backend/tests/live/__init__.py`):

```python
import pytest

from copilot.agent.pipeline import answer_question
from copilot.llm.provider import AnthropicProvider
from copilot.snowflake_client import SnowflakeClient

pytestmark = pytest.mark.live


def test_slice_end_to_end():
    r = answer_question(
        "Which 5 treatment centers had the most machine downtime hours in the last 90 days?",
        AnthropicProvider(), SnowflakeClient(role="COPILOT_APP_RO"))
    assert r.error_type is None, f"{r.error_type}: {r.answer}"
    assert r.sql and "FACT_MACHINE_UTILIZATION" in r.sql.upper()
    assert len(r.rows) == 5
    assert any(ch.isdigit() for ch in r.answer)
    print("\nSQL:\n", r.sql, "\nANSWER:\n", r.answer)
```

Run: `make test-live`
Expected: PASS with a sensible answer printed. This is the moment the vertical slice exists.

- [ ] **Step 6: Commit**

```bash
git add backend/src/copilot/agent backend/tests
git commit -m "feat: vertical-slice pipeline (retrieve -> SQL -> guard -> execute -> summarize)"
```

---

### Task 12: FastAPI app

**Files:**
- Create: `backend/src/copilot/api/__init__.py`, `backend/src/copilot/api/main.py`
- Test: `backend/tests/test_api.py`

**Interfaces:**
- Consumes: `pipeline.answer_question`, `AnthropicProvider`, `SnowflakeClient`.
- Produces: FastAPI `app`; `POST /chat` body `{"question": str, "conversation_id": str | null}` → `ChatResponse` JSON; `GET /healthz` → `{"status": "ok"}`. CORS open to `http://localhost:5173`. Providers built once at startup (module-level singletons via `_deps()`), overridable in tests through `app.state`.

- [ ] **Step 1: Write the failing test**

`backend/tests/test_api.py`:

```python
from fastapi.testclient import TestClient

from copilot.api.main import app
from tests.conftest import FakeProvider, FakeSnowflake


def _client():
    app.state.provider = FakeProvider()
    app.state.sf_ro = FakeSnowflake()
    return TestClient(app)


def test_healthz():
    assert _client().get("/healthz").json() == {"status": "ok"}


def test_chat_happy_path():
    r = _client().post("/chat", json={"question": "Which models?"})
    assert r.status_code == 200
    body = r.json()
    assert body["answer"] == "Here you go."
    assert "LIMIT 1000" in body["sql"]


def test_chat_empty_question_400():
    assert _client().post("/chat", json={"question": "  "}).status_code == 400
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/test_api.py -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement api/main.py**

```python
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from copilot.agent.pipeline import ChatResponse, answer_question

app = FastAPI(title="Analytics Copilot")
app.add_middleware(
    CORSMiddleware, allow_origins=["http://localhost:5173"],
    allow_methods=["*"], allow_headers=["*"],
)


class ChatRequest(BaseModel):
    question: str
    conversation_id: str | None = None


def _deps():
    if not hasattr(app.state, "provider"):
        from copilot.llm.provider import AnthropicProvider
        from copilot.snowflake_client import SnowflakeClient

        app.state.provider = AnthropicProvider()
        app.state.sf_ro = SnowflakeClient(role="COPILOT_APP_RO")
    return app.state.provider, app.state.sf_ro


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@app.post("/chat")
def chat(req: ChatRequest) -> ChatResponse:
    if not req.question.strip():
        raise HTTPException(status_code=400, detail="question is empty")
    provider, sf = _deps()
    return answer_question(req.question, provider, sf)
```

Create empty `backend/src/copilot/api/__init__.py`.

- [ ] **Step 4: Run tests**

Run: `cd backend && uv run pytest tests/test_api.py -v` → 3 PASS.
Then run everything: `make lint && make test` → all green.

- [ ] **Step 5: Manual smoke**

Run: `make api` then `curl -s localhost:8000/chat -H 'content-type: application/json' -d '{"question":"How many machines per model?"}' | head -c 600`
Expected: JSON with answer + SQL.

- [ ] **Step 6: Commit**

```bash
git add backend/src/copilot/api backend/tests/test_api.py
git commit -m "feat: FastAPI chat endpoint"
```

---

### Task 13: React chat UI

**Files:**
- Create: `frontend/` via Vite scaffold; then `frontend/src/{App.tsx, api.ts, types.ts, App.css}`, `frontend/src/App.test.tsx`, `frontend/.env.development`

**Interfaces:**
- Consumes: `POST /chat` (Task 12).
- Produces: chat page at `localhost:5173` — message list, input, per-answer collapsible SQL + result table (first 20 rows), loading state, error styling. `VITE_API_URL` env var (defaults to `http://localhost:8000`).

- [ ] **Step 1: Scaffold**

```bash
cd frontend 2>/dev/null || npm create vite@latest frontend -- --template react-ts
cd frontend && npm install && npm install -D vitest @testing-library/react @testing-library/jest-dom jsdom
```

Add to `frontend/package.json` scripts: `"test": "vitest"`. Create `frontend/vitest.config.ts`:

```ts
import { defineConfig } from "vitest/config";
export default defineConfig({ test: { environment: "jsdom", globals: true } });
```

Create `frontend/.env.development`: `VITE_API_URL=http://localhost:8000`

- [ ] **Step 2: types.ts + api.ts**

`frontend/src/types.ts`:

```ts
export interface ChatResponse {
  answer: string;
  sql: string | null;
  columns: string[];
  rows: unknown[][];
  assumptions: string[];
  error_type: string | null;
  retrieval_ms: number;
}

export interface Message {
  role: "user" | "assistant";
  text: string;
  data?: ChatResponse;
}
```

`frontend/src/api.ts`:

```ts
import type { ChatResponse } from "./types";

const BASE = import.meta.env.VITE_API_URL ?? "http://localhost:8000";

export async function sendChat(question: string): Promise<ChatResponse> {
  const res = await fetch(`${BASE}/chat`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ question }),
  });
  if (!res.ok) throw new Error(`API ${res.status}`);
  return res.json();
}
```

- [ ] **Step 3: App.tsx**

```tsx
import { useRef, useState } from "react";
import { sendChat } from "./api";
import type { Message } from "./types";
import "./App.css";

export default function App() {
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const bottom = useRef<HTMLDivElement>(null);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    const q = input.trim();
    if (!q || busy) return;
    setInput("");
    setMessages((m) => [...m, { role: "user", text: q }]);
    setBusy(true);
    try {
      const data = await sendChat(q);
      setMessages((m) => [...m, { role: "assistant", text: data.answer, data }]);
    } catch (err) {
      setMessages((m) => [...m, { role: "assistant", text: `Request failed: ${err}` }]);
    } finally {
      setBusy(false);
      bottom.current?.scrollIntoView({ behavior: "smooth" });
    }
  }

  return (
    <div className="shell">
      <header>
        <h1>Analytics Copilot</h1>
        <span className="sub">Ask about machines, centers, utilization, service tickets</span>
      </header>
      <main>
        {messages.length === 0 && (
          <div className="hint">
            Try: <em>Which 5 centers had the most downtime hours last quarter?</em>
          </div>
        )}
        {messages.map((m, i) => (
          <div key={i} className={`msg ${m.role} ${m.data?.error_type ? "err" : ""}`}>
            <p>{m.text}</p>
            {m.data?.sql && (
              <details>
                <summary>SQL</summary>
                <pre>{m.data.sql}</pre>
              </details>
            )}
            {m.data && m.data.rows.length > 0 && (
              <div className="tablewrap">
                <table>
                  <thead>
                    <tr>{m.data.columns.map((c) => <th key={c}>{c}</th>)}</tr>
                  </thead>
                  <tbody>
                    {m.data.rows.slice(0, 20).map((r, ri) => (
                      <tr key={ri}>{r.map((v, ci) => <td key={ci}>{String(v ?? "")}</td>)}</tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        ))}
        {busy && <div className="msg assistant busy">Thinking…</div>}
        <div ref={bottom} />
      </main>
      <form onSubmit={submit}>
        <input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder="Ask a question about the fleet…"
          disabled={busy}
        />
        <button disabled={busy || !input.trim()}>Send</button>
      </form>
    </div>
  );
}
```

- [ ] **Step 4: App.css** (replace the Vite default; also empty out `index.css` if it conflicts)

```css
* { box-sizing: border-box; }
body { margin: 0; font-family: -apple-system, "Segoe UI", Roboto, sans-serif;
       background: #0f1420; color: #e6e9f0; }
.shell { max-width: 860px; margin: 0 auto; display: flex; flex-direction: column;
         height: 100vh; padding: 0 16px; }
header { padding: 18px 4px 10px; border-bottom: 1px solid #232b3d; }
header h1 { margin: 0; font-size: 20px; }
header .sub { font-size: 13px; color: #8b94a7; }
main { flex: 1; overflow-y: auto; padding: 16px 0; }
.hint { color: #8b94a7; padding: 24px 8px; }
.msg { margin: 10px 0; padding: 12px 14px; border-radius: 12px; max-width: 92%; }
.msg.user { background: #1d4ed8; margin-left: auto; }
.msg.assistant { background: #1a2233; border: 1px solid #232b3d; }
.msg.err { border-color: #b4232355; }
.msg p { margin: 0; white-space: pre-wrap; line-height: 1.5; }
.msg details { margin-top: 8px; }
.msg summary { cursor: pointer; color: #8b94a7; font-size: 12px; }
.msg pre { background: #0b0f18; padding: 10px; border-radius: 8px; overflow-x: auto;
           font-size: 12px; }
.tablewrap { overflow-x: auto; margin-top: 8px; }
table { border-collapse: collapse; font-size: 12.5px; width: 100%; }
th, td { border: 1px solid #232b3d; padding: 5px 8px; text-align: left; }
th { background: #141b2b; }
form { display: flex; gap: 8px; padding: 12px 0 16px; border-top: 1px solid #232b3d; }
input { flex: 1; padding: 11px 14px; border-radius: 10px; border: 1px solid #232b3d;
        background: #141b2b; color: inherit; font-size: 14px; }
button { padding: 11px 20px; border-radius: 10px; border: 0; background: #1d4ed8;
         color: white; font-weight: 600; cursor: pointer; }
button:disabled { opacity: 0.5; cursor: default; }
.busy { color: #8b94a7; font-style: italic; }
```

In `frontend/src/main.tsx` remove the `import './index.css'` line (App.css owns styling).

- [ ] **Step 5: Smoke test `frontend/src/App.test.tsx`**

```tsx
import { render, screen } from "@testing-library/react";
import App from "./App";

test("renders title and input", () => {
  render(<App />);
  expect(screen.getByText("Analytics Copilot")).toBeDefined();
  expect(screen.getByPlaceholderText(/Ask a question/)).toBeDefined();
});
```

Run: `cd frontend && npm test -- --run` → PASS. `npm run build` → succeeds.

- [ ] **Step 6: Manual end-to-end**

With `make api` running: `make web`, open http://localhost:5173, ask "Which 5 centers had the most downtime hours last quarter?" — answer + SQL + table render.

- [ ] **Step 7: Commit**

```bash
git add frontend
git commit -m "feat: React chat UI (answer, SQL panel, result table)"
```

---

### Task 14: End-of-phase verification + tag

**Files:**
- Create: `scripts/demo_check.py`

**Interfaces:**
- Consumes: running API (Task 12).
- Produces: scripted proof the slice answers the 5 demo questions; git tag `v0.1-slice`.

- [ ] **Step 1: Create `scripts/demo_check.py`**

```python
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
```

- [ ] **Step 2: Run it against the live API**

Run: `make api` (background) then `cd backend && uv run python ../scripts/demo_check.py`
Expected: 5× `[OK]` with sensible answers. Any `ERR` → fix the prompt/schema-card wording (most misses are card wording, not code) and rerun.

- [ ] **Step 3: Full test suite + lint one more time**

Run: `make lint && make test && make test-live`
Expected: all green.

- [ ] **Step 4: Commit + tag**

```bash
git add scripts/demo_check.py
git commit -m "feat: demo question scorecard script"
git tag v0.1-slice
```

---

## Self-Review (done during planning)

- **Spec coverage (Phase 1 scope):** scaffold ✓ (T1), seed ✓ (T2), Snowflake bootstrap + keypair ✓ (T3), config/client ✓ (T4), bronze ✓ (T5), dbt silver+gold+tests+clustering ✓ (T6), governance/masking ✓ (T7), provider + schema-checked extraction ✓ (T8), SQL guard ✓ (T9), AI library + vector retrieval + fallback ✓ (T10), prompts + slice pipeline ✓ (T11), API ✓ (T12), React chat ✓ (T13), demo verification ✓ (T14). Deferred to Phase 2+ by design: LangGraph, MCP, auth/RBAC endpoints, feedback UI, REQUEST_LOG writes, evals, AWS/Terraform/CloudWatch, Admin Console.
- **Placeholder scan:** none — every step has runnable content.
- **Type consistency:** `run_query -> tuple[list[str], list[tuple]]` used by load_bronze/verify_governance/retrieval/pipeline ✓; `LLMResult.value` duck-typed per schema ✓; `ChatResponse` field names match React `types.ts` ✓; `RetrievedContext.mode` set in both paths ✓.
- **Known risk accepted:** Cortex embed model name availability varies by region (`snowflake-arctic-embed-m-v1.5` vs `-m`); Task 10 Step 7 verifies live and the keyword fallback keeps the slice working regardless.
```
