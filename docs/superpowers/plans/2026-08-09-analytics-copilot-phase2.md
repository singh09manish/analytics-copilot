# Analytics Copilot — Phase 2 (Agent + MCP + Auth) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the linear pipeline with a LangGraph agent (intent routing, self-repair, multi-turn memory), execute SQL through a real MCP tool server, and gate the app behind JWT auth with analyst/admin roles mapped to masked/unmasked Snowflake sessions — plus request logging, a feedback endpoint, and the matching React login/feedback UI.

**Architecture:** The public contract is untouched: `answer_question(...) -> ChatResponse` never raises, `/chat` returns the same JSON (+2 additive fields). Internally a `StateGraph` routes plan → retrieve → generate → validate → execute → summarize with a conditional repair edge and an `InMemorySaver` checkpointer keyed by `conversation_id`. The executor calls `run_query` on an MCP server (stdio subprocess) through a sync client wrapper; the server re-validates SQL (defense layer 2). Auth is PyJWT HS256 + bcrypt demo users; the JWT role picks the Snowflake session role (analyst → `COPILOT_APP_RO` masked, admin → `COPILOT_ADMIN` unmasked).

**Tech Stack:** Phase 1 stack + `langgraph`, `mcp` (FastMCP), `pyjwt`, `bcrypt`.

## Global Constraints

- Phase 1 interfaces are LOCKED — do not change signatures of: `SnowflakeClient.run_query(sql, params=()) -> tuple[list[str], list[tuple]]`, `LLMProvider.structured/text -> LLMResult`, `retrieve(question, sf, k_cards=3, k_terms=5) -> RetrievedContext`, `sql_guard.validate(sql) -> str` / `SqlGuardError(.reason)`, `prompts.PROMPT_VERSION/sql_system/summarize_system`.
- `ChatResponse` gains ONLY additive fields: `intent: str | None = None`, `request_id: str | None = None`. `answer_question` gains keyword-only `conversation_id: str | None = None`. It still NEVER raises.
- Roles map exactly: JWT role `analyst` → Snowflake `COPILOT_APP_RO`; `admin` → `COPILOT_ADMIN`. `/feedback` and REQUEST_LOG writes use `COPILOT_APP_WRITER`.
- Ops table columns are FIXED (created in bootstrap.sql): `FEEDBACK(feedback_id, conversation_id, request_id, rating, comment, prompt_version, created_at)`, `REQUEST_LOG(request_id, conversation_id, user_role, question, intent, sql_text, status, error_type, e2e_ms, retrieval_ms, tokens_in, tokens_out, prompt_version, created_at)`.
- MCP server must NOT trust layer 1: it re-runs `sql_guard.validate` AND rejects side-effecting scalar functions (`SYSTEM$`, `GET_DDL`) — per the Phase 1 threat-model note.
- Secrets only via `.env` (JWT secret, demo password hashes); never committed. `uv` lives at `~/.local/bin` (export PATH in shells).
- All unit tests run hermetically (no network/credentials); `live`-marked tests need `.env`. ruff scope: `src tests ../data ../scripts ../warehouse ../mcp_server`.
- Commits: conventional prefixes; each task ends in a commit. Phase 2 ends at tag `v0.2-agent`.

## File Structure

```
backend/src/copilot/
├── auth.py                  # NEW: hashing, JWT create/verify, FastAPI role dependency
├── agent/
│   ├── graph.py             # NEW: StateGraph, nodes, repair edge, checkpointer
│   ├── pipeline.py          # MODIFY: answer_question delegates to graph; ChatResponse +2 fields
│   └── prompts.py           # MODIFY: add plan_system/glossary_system/history-aware user builders
├── mcp_client.py            # NEW: sync wrapper over async MCP stdio client
├── request_log.py           # NEW: REQUEST_LOG insert helper (never raises)
└── api/main.py              # MODIFY: /auth/login, auth deps, role-scoped sf, /feedback, logging
mcp_server/server.py         # NEW: FastMCP with 4 tools
scripts/gen_demo_users.py    # NEW: prints bcrypt hashes for .env
backend/tests/               # test_auth.py, test_graph.py, test_mcp_server.py,
                             # test_mcp_client.py, test_request_log.py; test_api.py updated
frontend/src/
├── auth.ts                  # NEW: token storage, login/logout API
├── Login.tsx                # NEW: login form
├── App.tsx                  # MODIFY: auth gate, role badge, conversation_id, feedback UI
├── api.ts                   # MODIFY: Authorization header, sendFeedback
└── types.ts                 # MODIFY: +intent/request_id, LoginResponse
```

---

### Task 1: Dependencies, settings, demo-user generator

**Files:**
- Modify: `backend/pyproject.toml` (dependencies)
- Modify: `backend/src/copilot/config.py`
- Create: `scripts/gen_demo_users.py`
- Modify: `Makefile` (lint scope + `mcp-server` target)
- Modify: `.github/workflows/ci.yml` (lint scope)
- Modify: `.env.example`

**Interfaces:**
- Produces: `Settings` gains `jwt_secret: str = "dev-secret-change-me"`, `jwt_ttl_hours: int = 8`, `demo_analyst_password_hash: str = ""`, `demo_admin_password_hash: str = ""`, `use_mcp: bool = True`. New deps importable: `langgraph`, `mcp`, `jwt` (pyjwt), `bcrypt`.

- [ ] **Step 1: Add dependencies**

In `backend/pyproject.toml` `[project].dependencies` append:

```toml
    "langgraph>=0.2",
    "mcp>=1.2",
    "pyjwt>=2.9",
    "bcrypt>=4.2",
```

Run: `cd backend && ~/.local/bin/uv sync` → resolves.

- [ ] **Step 2: Extend Settings**

Add to the `Settings` class in `backend/src/copilot/config.py` (after `snowflake_role`):

```python
    jwt_secret: str = "dev-secret-change-me"
    jwt_ttl_hours: int = 8
    demo_analyst_password_hash: str = ""
    demo_admin_password_hash: str = ""
    use_mcp: bool = True
```

- [ ] **Step 3: Create `scripts/gen_demo_users.py`**

```python
"""Generate bcrypt hashes for the two demo accounts. Paste output into .env."""
import getpass

import bcrypt


def main() -> None:
    for account in ("analyst", "admin"):
        pw = getpass.getpass(f"Password for {account}@demo: ")
        h = bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode()
        print(f"DEMO_{account.upper()}_PASSWORD_HASH={h}")
    print("JWT_SECRET=" + bcrypt.gensalt().hex())


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Extend .env.example, Makefile, CI**

`.env.example` — append:

```bash
# Auth (generate with: cd backend && uv run python ../scripts/gen_demo_users.py)
JWT_SECRET=dev-secret-change-me
DEMO_ANALYST_PASSWORD_HASH=
DEMO_ADMIN_PASSWORD_HASH=
USE_MCP=true
```

Makefile: change lint target to `$(UV) run ruff check src tests ../data ../scripts ../warehouse ../mcp_server` and add:

```makefile
mcp-server:
	$(UV) run python ../mcp_server/server.py
```

(add `mcp-server` to `.PHONY`, and while touching it add the four targets missing from `.PHONY`: `load-bronze dbt-run dbt-test ai-library` — clears a Phase 1 deferred minor). CI backend lint line matches the new scope. Create empty `mcp_server/__init__.py` placeholder? No — `mcp_server/` gets its file in Task 5; ruff tolerates a missing path only if it exists, so create the directory with a `.gitkeep`-style empty `mcp_server/server.py` stub? NO — instead keep `../mcp_server` OUT of lint scope until Task 5 creates it; Task 5 adds it to Makefile+CI lint scope. (Makefile lint stays Phase 1 scope in this task.)

- [ ] **Step 5: Verify + commit**

Run: `cd backend && ~/.local/bin/uv run python -c "import langgraph, mcp, jwt, bcrypt; print('deps ok')"` then `make lint && make test` (48 passed).

```bash
git add backend/pyproject.toml backend/uv.lock backend/src/copilot/config.py scripts/gen_demo_users.py Makefile .github/workflows/ci.yml .env.example
git commit -m "chore: phase 2 deps (langgraph, mcp, pyjwt, bcrypt) + auth settings"
```

**USER ACTION after this task:** run `cd backend && uv run python ../scripts/gen_demo_users.py`, choose two demo passwords, paste the three printed lines into `.env`.

---

### Task 2: Auth module (TDD)

**Files:**
- Create: `backend/src/copilot/auth.py`
- Test: `backend/tests/test_auth.py`

**Interfaces:**
- Produces: `auth.authenticate(email: str, password: str) -> str | None` (returns role "analyst"/"admin" or None); `auth.create_token(role: str, email: str) -> str`; `auth.decode_token(token: str) -> dict` (raises `AuthError` on invalid/expired); `auth.AuthError(Exception)`; FastAPI dependency `auth.require_role(request: Request) -> str` reading `Authorization: Bearer <token>`, raising HTTPException 401.

- [ ] **Step 1: Write failing tests** — `backend/tests/test_auth.py`:

```python
import bcrypt
import pytest
from fastapi import HTTPException

from copilot import auth


@pytest.fixture(autouse=True)
def demo_hashes(monkeypatch):
    h = bcrypt.hashpw(b"pw123", bcrypt.gensalt()).decode()
    s = auth.get_settings()
    monkeypatch.setattr(s, "demo_analyst_password_hash", h)
    monkeypatch.setattr(s, "demo_admin_password_hash", h)
    monkeypatch.setattr(s, "jwt_secret", "test-secret")


def test_authenticate_roles():
    assert auth.authenticate("analyst@demo", "pw123") == "analyst"
    assert auth.authenticate("admin@demo", "pw123") == "admin"
    assert auth.authenticate("analyst@demo", "wrong") is None
    assert auth.authenticate("nobody@demo", "pw123") is None


def test_token_roundtrip():
    token = auth.create_token("admin", "admin@demo")
    claims = auth.decode_token(token)
    assert claims["role"] == "admin" and claims["sub"] == "admin@demo"


def test_decode_rejects_garbage_and_wrong_secret():
    with pytest.raises(auth.AuthError):
        auth.decode_token("not.a.token")
    import jwt as pyjwt
    forged = pyjwt.encode({"role": "admin", "sub": "x"}, "other-secret", algorithm="HS256")
    with pytest.raises(auth.AuthError):
        auth.decode_token(forged)


def test_require_role_dependency():
    token = auth.create_token("analyst", "analyst@demo")

    class FakeRequest:
        def __init__(self, header):
            self.headers = {"authorization": header} if header else {}

    assert auth.require_role(FakeRequest(f"Bearer {token}")) == "analyst"
    for bad in (None, "Bearer nope", "Basic abc"):
        with pytest.raises(HTTPException) as e:
            auth.require_role(FakeRequest(bad))
        assert e.value.status_code == 401
```

- [ ] **Step 2: Run to verify FAIL** — `cd backend && uv run pytest tests/test_auth.py -v` → module missing.

- [ ] **Step 3: Implement `backend/src/copilot/auth.py`**

```python
"""Demo auth: two fixed accounts, bcrypt hashes from settings, HS256 JWTs."""
from datetime import UTC, datetime, timedelta

import bcrypt
import jwt

from copilot.config import get_settings


class AuthError(Exception):
    pass


_ACCOUNTS = {"analyst@demo": ("analyst", "demo_analyst_password_hash"),
             "admin@demo": ("admin", "demo_admin_password_hash")}


def authenticate(email: str, password: str) -> str | None:
    entry = _ACCOUNTS.get(email.strip().lower())
    if entry is None:
        return None
    role, hash_field = entry
    stored = getattr(get_settings(), hash_field)
    if not stored:
        return None
    if bcrypt.checkpw(password.encode(), stored.encode()):
        return role
    return None


def create_token(role: str, email: str) -> str:
    s = get_settings()
    payload = {"sub": email, "role": role,
               "exp": datetime.now(UTC) + timedelta(hours=s.jwt_ttl_hours)}
    return jwt.encode(payload, s.jwt_secret, algorithm="HS256")


def decode_token(token: str) -> dict:
    try:
        return jwt.decode(token, get_settings().jwt_secret, algorithms=["HS256"])
    except jwt.PyJWTError as e:
        raise AuthError(str(e)) from e


def require_role(request) -> str:
    """FastAPI dependency: returns 'analyst' | 'admin' or raises 401."""
    from fastapi import HTTPException

    header = request.headers.get("authorization", "")
    if not header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    try:
        return decode_token(header.removeprefix("Bearer "))["role"]
    except AuthError as e:
        raise HTTPException(status_code=401, detail=f"invalid token: {e}") from e
```

- [ ] **Step 4: Run to PASS** — `uv run pytest tests/test_auth.py -v` → 4 PASS. Note: the fixture monkeypatches the cached Settings instance's attributes (works because `get_settings()` returns a singleton — no cache clearing needed).

- [ ] **Step 5: Commit**

```bash
git add backend/src/copilot/auth.py backend/tests/test_auth.py
git commit -m "feat: JWT auth with bcrypt demo accounts and role dependency"
```

---

### Task 3: Prompts for plan/glossary/history (TDD-light)

**Files:**
- Modify: `backend/src/copilot/agent/prompts.py`
- Test: `backend/tests/test_prompts.py`

**Interfaces:**
- Produces: `PROMPT_VERSION = "v2"`; `plan_system() -> str`; `glossary_system() -> str`; `user_with_history(question: str, history: list[tuple[str, str]]) -> str` (formats last 3 Q/A turns + current question); existing `sql_system(context)`/`summarize_system()` unchanged.

- [ ] **Step 1: Failing tests** — `backend/tests/test_prompts.py`:

```python
from copilot.agent import prompts


def test_prompt_version_bumped():
    assert prompts.PROMPT_VERSION == "v2"


def test_plan_system_mentions_intents():
    s = prompts.plan_system()
    for intent in ("data_query", "glossary_lookup", "smalltalk", "unsupported"):
        assert intent in s


def test_user_with_history_includes_last_turns():
    hist = [("q1", "a1"), ("q2", "a2"), ("q3", "a3"), ("q4", "a4")]
    out = prompts.user_with_history("current?", hist)
    assert "current?" in out and "q4" in out and "q2" in out
    assert "q1" not in out  # only last 3 turns
```

- [ ] **Step 2: FAIL** — `uv run pytest tests/test_prompts.py -v`.

- [ ] **Step 3: Implement** — in `prompts.py`: change `PROMPT_VERSION = "v2"` and append:

```python
def plan_system() -> str:
    return f"""You classify a user's message for an analytics copilot over a
medical-device warehouse (machines, treatment centers, utilization, service tickets).
Today is {TODAY}. Classify intent as exactly one of:
- data_query: answerable by querying the warehouse
- glossary_lookup: asks what a business term/metric means
- smalltalk: greeting/chitchat/thanks
- unsupported: anything else (other topics, actions, writes)
Also extract entity strings mentioned (models, regions, severities, metrics)."""


def glossary_system() -> str:
    return f"""You are an analytics copilot. Today is {TODAY}. Answer the user's
question about a business term using ONLY the glossary entries provided. Quote the
definition, name the underlying tables, and keep it to 2-3 sentences. If the term
is not in the glossary, say so and suggest the closest term that is."""


def user_with_history(question: str, history: list[tuple[str, str]]) -> str:
    if not history:
        return question
    turns = "\n".join(f"Q: {q}\nA: {a[:300]}" for q, a in history[-3:])
    return f"Recent conversation:\n{turns}\n\nCurrent question: {question}"
```

- [ ] **Step 4: PASS + full suite** — `uv run pytest -q` (all green; nothing else asserts on PROMPT_VERSION value).

- [ ] **Step 5: Commit** — `git add -A backend/src/copilot/agent/prompts.py backend/tests/test_prompts.py && git commit -m "feat: v2 prompts (intent planning, glossary answers, history)"`

---

### Task 4: LangGraph agent graph (TDD)

**Files:**
- Create: `backend/src/copilot/agent/graph.py`
- Modify: `backend/src/copilot/agent/pipeline.py`
- Test: `backend/tests/test_graph.py`

**Interfaces:**
- Consumes: everything from Phase 1 + Task 3 prompts + `QueryPlan` schema.
- Produces: `graph.build_graph(provider, sf, executor=None) -> CompiledStateGraph` (executor: optional callable `(sql: str) -> tuple[list[str], list[tuple]]`, defaults to `sf.run_query` — Task 6 injects the MCP client); `pipeline.answer_question(question, provider, sf, *, conversation_id: str | None = None, executor=None) -> ChatResponse` — same never-raises contract, now graph-backed with per-`conversation_id` memory. `ChatResponse` gains `intent: str | None = None`, `request_id: str | None = None`.

- [ ] **Step 1: Failing tests** — `backend/tests/test_graph.py`:

```python
from copilot.agent.pipeline import answer_question
from copilot.llm.provider import LLMResult
from copilot.llm.schemas import QueryPlan, SqlDraft
from tests.conftest import FakeProvider, FakeSnowflake


class PlanningFakeProvider(FakeProvider):
    """FakeProvider that also answers plan-node calls; scriptable intent."""

    def __init__(self, intent="data_query", **kw):
        super().__init__(**kw)
        self.intent = intent
        self.structured_calls = []

    def structured(self, system, user, schema, max_tokens=1500):
        self.structured_calls.append((schema.__name__, user))
        if schema is QueryPlan:
            return LLMResult(value=QueryPlan(intent=self.intent, entities=[]),
                             tokens_in=5, tokens_out=2)
        return super().structured(system, user, schema, max_tokens)


def test_data_query_end_to_end():
    r = answer_question("Which models?", PlanningFakeProvider(), FakeSnowflake())
    assert r.error_type is None
    assert r.intent == "data_query"
    assert "LIMIT 1000" in r.sql
    assert r.rows == [["TrueBeam"], ["Halcyon"]]


def test_smalltalk_never_generates_sql():
    p = PlanningFakeProvider(intent="smalltalk")
    r = answer_question("hey there!", p, FakeSnowflake())
    assert r.sql is None and r.intent == "smalltalk"
    assert not any(n == "SqlDraft" for n, _ in p.structured_calls)


def test_glossary_lookup_answers_without_sql():
    r = answer_question("What does MTTR mean?",
                        PlanningFakeProvider(intent="glossary_lookup"), FakeSnowflake())
    assert r.sql is None and r.intent == "glossary_lookup"
    assert r.answer  # text answer produced from glossary context


def test_repair_loop_retries_failed_sql_once():
    class FlakySnowflake(FakeSnowflake):
        def __init__(self):
            super().__init__()
            self.exec_attempts = 0

        def run_query(self, sql, params=()):
            if "GOLD.DIM_MACHINE" in sql and "VECTOR" not in sql:
                self.exec_attempts += 1
                if self.exec_attempts == 1:
                    raise RuntimeError("SQL compilation error: invalid identifier 'MODELL'")
            return super().run_query(sql, params)

    sf = FlakySnowflake()
    r = answer_question("Which models?", PlanningFakeProvider(), sf)
    assert sf.exec_attempts == 2  # failed once, repaired, succeeded
    assert r.error_type is None


def test_multi_turn_memory_feeds_history():
    p = PlanningFakeProvider()
    sf = FakeSnowflake()
    answer_question("Which models?", p, sf, conversation_id="c1")
    answer_question("now break that down by region", p, sf, conversation_id="c1")
    sql_user_inputs = [u for n, u in p.structured_calls if n == "SqlDraft"]
    assert "Which models?" in sql_user_inputs[-1]  # history reached the SQL prompt


def test_never_raises_on_plan_failure():
    class ExplodingProvider(PlanningFakeProvider):
        def structured(self, system, user, schema, max_tokens=1500):
            raise ValueError("LLM output schema-invalid twice for QueryPlan")

    r = answer_question("Which models?", ExplodingProvider(), FakeSnowflake())
    assert r.error_type == "llm"
```

- [ ] **Step 2: FAIL** — `uv run pytest tests/test_graph.py -v`.

- [ ] **Step 3: Implement `backend/src/copilot/agent/graph.py`**

```python
"""LangGraph agent: plan -> (route) -> retrieve -> generate -> validate -> execute
-> summarize, with one repair cycle and per-conversation memory."""
import csv
import io
import time
from typing import Any, TypedDict

from langgraph.graph import END, StateGraph

from copilot.agent import prompts
from copilot.llm.schemas import QueryPlan, SqlDraft
from copilot.retrieval import RetrievedContext, retrieve
from copilot.sql_guard import SqlGuardError, validate

MAX_SUMMARY_ROWS = 50
SCOPE_MESSAGE = ("I answer questions about the analytics warehouse - machines, "
                 "treatment centers, utilization, and service tickets. Try asking "
                 "about downtime, delivered fractions, or open tickets.")


class AgentState(TypedDict, total=False):
    question: str
    history: list  # [(q, a), ...] persisted by checkpointer
    intent: str
    context: dict  # RetrievedContext.model_dump()
    draft_sql: str
    assumptions: list
    safe_sql: str
    columns: list
    rows: list
    exec_error: str
    repair_count: int
    answer: str
    error_type: str
    tokens_in: int
    tokens_out: int
    retrieval_ms: int


def _rows_as_csv(columns: list, rows: list) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(columns)
    w.writerows(rows[:MAX_SUMMARY_ROWS])
    return buf.getvalue()


def build_graph(provider, sf, executor=None):
    run_sql = executor or sf.run_query

    def plan(state: AgentState) -> dict:
        user = prompts.user_with_history(state["question"], state.get("history", []))
        res = provider.structured(system=prompts.plan_system(), user=user,
                                  schema=QueryPlan, max_tokens=300)
        return {"intent": res.value.intent,
                "tokens_in": state.get("tokens_in", 0) + res.tokens_in,
                "tokens_out": state.get("tokens_out", 0) + res.tokens_out}

    def scope_reply(state: AgentState) -> dict:
        if state["intent"] == "smalltalk":
            return {"answer": "Hi! " + SCOPE_MESSAGE}
        return {"answer": SCOPE_MESSAGE}

    def do_retrieve(state: AgentState) -> dict:
        ctx = retrieve(state["question"], sf)
        return {"context": ctx.model_dump(), "retrieval_ms": ctx.retrieval_ms}

    def glossary_answer(state: AgentState) -> dict:
        ctx = RetrievedContext.model_validate(state["context"])
        glossary = "\n".join(f"- {g}" for g in ctx.glossary)
        res = provider.text(system=prompts.glossary_system(),
                            user=f"Glossary entries:\n{glossary}\n\n"
                                 f"Question: {state['question']}")
        return {"answer": res.value,
                "tokens_in": state.get("tokens_in", 0) + res.tokens_in,
                "tokens_out": state.get("tokens_out", 0) + res.tokens_out}

    def generate(state: AgentState) -> dict:
        ctx = RetrievedContext.model_validate(state["context"])
        user = prompts.user_with_history(state["question"], state.get("history", []))
        if state.get("exec_error"):
            user += (f"\n\nYour previous SQL failed with this Snowflake error - "
                     f"fix it:\n{state['exec_error']}\nPrevious SQL:\n{state['draft_sql']}")
        res = provider.structured(system=prompts.sql_system(ctx), user=user,
                                  schema=SqlDraft)
        draft: SqlDraft = res.value
        return {"draft_sql": draft.sql, "assumptions": draft.assumptions,
                "tokens_in": state.get("tokens_in", 0) + res.tokens_in,
                "tokens_out": state.get("tokens_out", 0) + res.tokens_out}

    def do_validate(state: AgentState) -> dict:
        return {"safe_sql": validate(state["draft_sql"])}

    def execute(state: AgentState) -> dict:
        try:
            columns, rows = run_sql(state["safe_sql"])
            return {"columns": list(columns), "rows": [list(r) for r in rows],
                    "exec_error": ""}
        except Exception as e:  # noqa: BLE001 — routed to repair or graceful error
            return {"exec_error": str(e)[:500],
                    "repair_count": state.get("repair_count", 0) + 1}

    def summarize(state: AgentState) -> dict:
        res = provider.text(
            system=prompts.summarize_system(),
            user=f"Question: {state['question']}\n\nSQL:\n{state['safe_sql']}\n\n"
                 f"Results (CSV, first {MAX_SUMMARY_ROWS} rows):\n"
                 f"{_rows_as_csv(state['columns'], state['rows'])}")
        return {"answer": res.value,
                "tokens_in": state.get("tokens_in", 0) + res.tokens_in,
                "tokens_out": state.get("tokens_out", 0) + res.tokens_out}

    def remember(state: AgentState) -> dict:
        history = list(state.get("history", []))
        history.append((state["question"], state.get("answer", "")[:500]))
        return {"history": history[-6:]}

    g = StateGraph(AgentState)
    g.add_node("plan", plan)
    g.add_node("scope_reply", scope_reply)
    g.add_node("retrieve", do_retrieve)
    g.add_node("glossary_answer", glossary_answer)
    g.add_node("generate", generate)
    g.add_node("validate", do_validate)
    g.add_node("execute", execute)
    g.add_node("summarize", summarize)
    g.add_node("remember", remember)

    g.set_entry_point("plan")
    g.add_conditional_edges("plan", lambda s: s["intent"], {
        "data_query": "retrieve", "glossary_lookup": "retrieve",
        "smalltalk": "scope_reply", "unsupported": "scope_reply"})
    g.add_conditional_edges("retrieve", lambda s: s["intent"], {
        "data_query": "generate", "glossary_lookup": "glossary_answer"})
    g.add_edge("generate", "validate")
    g.add_edge("validate", "execute")
    g.add_conditional_edges(
        "execute",
        lambda s: "repair" if s.get("exec_error") and s.get("repair_count", 0) <= 1
        else ("failed" if s.get("exec_error") else "ok"),
        {"repair": "generate", "ok": "summarize", "failed": "remember"})
    g.add_edge("scope_reply", "remember")
    g.add_edge("glossary_answer", "remember")
    g.add_edge("summarize", "remember")
    g.add_edge("remember", END)
    return g.compile(checkpointer=_CHECKPOINTER)


from langgraph.checkpoint.memory import InMemorySaver  # noqa: E402

_CHECKPOINTER = InMemorySaver()
```

- [ ] **Step 4: Rewire `pipeline.py`** — replace `answer_question` (keep `ChatResponse` in this file, add the two new fields; keep the module's existing imports that remain used, drop dead ones):

```python
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
    intent: str | None = None
    request_id: str | None = None


def answer_question(question: str, provider: LLMProvider, sf: SnowflakeClient, *,
                    conversation_id: str | None = None, executor=None) -> ChatResponse:
    import uuid

    from copilot.agent.graph import build_graph
    from copilot.sql_guard import SqlGuardError

    request_id = uuid.uuid4().hex
    thread = conversation_id or request_id
    graph = build_graph(provider, sf, executor=executor)
    try:
        state = graph.invoke(
            {"question": question, "intent": "", "context": {}, "draft_sql": "",
             "assumptions": [], "safe_sql": "", "columns": [], "rows": [],
             "exec_error": "", "repair_count": 0, "answer": "", "error_type": "",
             "tokens_in": 0, "tokens_out": 0, "retrieval_ms": 0},
            config={"configurable": {"thread_id": thread}})
    except SqlGuardError as e:
        return ChatResponse(
            answer=f"I generated a query the safety rules rejected ({e.reason}). "
                   "Try rephrasing your question.",
            error_type="validation", request_id=request_id)
    except ValueError:
        return ChatResponse(
            answer="I couldn't turn that into a query. Try rephrasing with the "
                   "metric and time range you care about.",
            error_type="llm", request_id=request_id)
    except Exception:  # noqa: BLE001 — total outage must degrade, not crash
        return ChatResponse(
            answer="I couldn't reach the warehouse to look up context. Please try "
                   "again in a moment.",
            error_type="snowflake", request_id=request_id)
    if state.get("exec_error"):
        return ChatResponse(
            answer="The query failed against the warehouse even after a retry. "
                   "Try asking a bit differently.",
            sql=state.get("safe_sql") or state.get("draft_sql"),
            error_type="snowflake", intent=state.get("intent"),
            retrieval_ms=state.get("retrieval_ms", 0), request_id=request_id,
            tokens_in=state.get("tokens_in", 0), tokens_out=state.get("tokens_out", 0))
    is_data = state.get("intent") == "data_query"
    return ChatResponse(
        answer=state.get("answer", ""),
        sql=state.get("safe_sql") or None if is_data else None,
        columns=state.get("columns", []) if is_data else [],
        rows=state.get("rows", [])[:200] if is_data else [],
        assumptions=state.get("assumptions", []) if is_data else [],
        intent=state.get("intent"), retrieval_ms=state.get("retrieval_ms", 0),
        tokens_in=state.get("tokens_in", 0), tokens_out=state.get("tokens_out", 0),
        request_id=request_id)
```

Note: summarize-failure graceful degradation now lives in the graph path — if `provider.text` raises inside `summarize`, it propagates to the generic handler. To preserve Phase 1's "keep the data" behavior, wrap the `provider.text` call inside the `summarize` node:

```python
    def summarize(state: AgentState) -> dict:
        try:
            res = provider.text(...)  # as above
            return {"answer": res.value, "tokens_in": ..., "tokens_out": ...}
        except Exception:  # noqa: BLE001 — keep the data even if summarization fails
            return {"answer": "I ran the query successfully but couldn't generate a "
                              "summary. The results are shown below.",
                    "error_type": "llm"}
```

and propagate `error_type` from state into the final ChatResponse when set (`error_type=state.get("error_type") or None`). Use exactly this in the implementation (the test file's `test_summarize_failure_keeps_data` from Phase 1 must keep passing).

- [ ] **Step 5: PASS** — `uv run pytest tests/test_graph.py tests/test_pipeline.py -v`. The three Phase 1 pipeline tests must pass unchanged EXCEPT `test_happy_path_returns_answer_sql_rows` asserts `r.tokens_in == 20` — with the plan node the count grows; update that single assertion to `r.tokens_in >= 20` and note it in the report. Then full `uv run pytest -q`.

- [ ] **Step 6: Commit** — `git add -A backend && git commit -m "feat: LangGraph agent graph (intent routing, repair loop, conversation memory)"`

---

### Task 5: MCP server (TDD)

**Files:**
- Create: `mcp_server/server.py`
- Test: `backend/tests/test_mcp_server.py`
- Modify: `Makefile` + `.github/workflows/ci.yml` (add `../mcp_server` to ruff scope)

**Interfaces:**
- Produces: FastMCP stdio server `analytics-copilot-snowflake` with tools: `list_tables() -> list[dict]` (name + card summary), `describe_table(table_name: str) -> str` (full card), `search_glossary(question: str, k: int = 5) -> list[str]`, `run_query(sql: str) -> dict` (`{"columns": [...], "rows": [...]}`) — re-validates via sql_guard + denies `SYSTEM$`/`GET_DDL` + runs as `COPILOT_APP_RO`. Pure functions importable for tests: `_run_query_impl(sql, sf)`, `_deny_side_effects(sql)`.

- [ ] **Step 1: Failing tests** — `backend/tests/test_mcp_server.py`:

```python
import importlib.util
import sys
from pathlib import Path

import pytest

SERVER = Path(__file__).parents[2] / "mcp_server" / "server.py"
spec = importlib.util.spec_from_file_location("mcp_srv", SERVER)
mcp_srv = importlib.util.module_from_spec(spec)
sys.modules["mcp_srv"] = mcp_srv
spec.loader.exec_module(mcp_srv)

from tests.conftest import FakeSnowflake  # noqa: E402


def test_run_query_revalidates_and_executes():
    out = mcp_srv._run_query_impl("SELECT model FROM GOLD.DIM_MACHINE", FakeSnowflake())
    assert out["columns"] == ["MODEL"]
    assert out["rows"] == [["TrueBeam"], ["Halcyon"]]


def test_run_query_rejects_bad_sql_at_layer2():
    with pytest.raises(ValueError, match="rejected"):
        mcp_srv._run_query_impl("DROP TABLE GOLD.DIM_MACHINE", FakeSnowflake())


@pytest.mark.parametrize("evil", [
    "SELECT SYSTEM$CANCEL_ALL_QUERIES(123)",
    "SELECT GET_DDL('TABLE', 'GOLD.DIM_MACHINE')",
])
def test_side_effecting_scalars_denied(evil):
    with pytest.raises(ValueError, match="side-effect"):
        mcp_srv._run_query_impl(evil, FakeSnowflake())


def test_describe_table_returns_card():
    card = mcp_srv._describe_table_impl("GOLD.DIM_MACHINE", FakeSnowflake())
    assert "machine" in card.lower()
```

- [ ] **Step 2: FAIL**, then **Step 3: Implement `mcp_server/server.py`**

```python
"""MCP tool server for the Analytics Copilot warehouse (defense layer 2).

Run standalone (stdio):  uv run python ../mcp_server/server.py
Claude Desktop config:   command=<repo>/backend/.venv/bin/python, args=[<this file>]
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "backend" / "src"))

from mcp.server.fastmcp import FastMCP  # noqa: E402

from copilot.sql_guard import SqlGuardError, validate  # noqa: E402

SIDE_EFFECT_PATTERN = re.compile(r"SYSTEM\$|GET_DDL\s*\(", re.IGNORECASE)

mcp = FastMCP("analytics-copilot-snowflake")


def _sf():
    from copilot.snowflake_client import SnowflakeClient

    return SnowflakeClient(role="COPILOT_APP_RO")


def _deny_side_effects(sql: str) -> None:
    if SIDE_EFFECT_PATTERN.search(sql):
        raise ValueError("side-effecting or metadata functions are not allowed")


def _run_query_impl(sql: str, sf) -> dict:
    _deny_side_effects(sql)
    try:
        safe = validate(sql)
    except SqlGuardError as e:
        raise ValueError(f"rejected by SQL guard: {e.reason}") from e
    columns, rows = sf.run_query(safe)
    return {"columns": list(columns), "rows": [list(r) for r in rows[:1000]]}


def _describe_table_impl(table_name: str, sf) -> str:
    _, rows = sf.run_query(
        "SELECT card FROM MEDTECH_ANALYTICS.COPILOT.SCHEMA_CARDS "
        "WHERE UPPER(table_name) = UPPER(%s)", (table_name,))
    return rows[0][0] if rows else f"No schema card found for {table_name}"


@mcp.tool()
def run_query(sql: str) -> dict:
    """Execute one read-only SELECT against GOLD/COPILOT. Validated server-side."""
    return _run_query_impl(sql, _sf())


@mcp.tool()
def list_tables() -> list[dict]:
    """List queryable gold tables with one-line summaries."""
    _, rows = _sf().run_query(
        "SELECT table_name, card FROM MEDTECH_ANALYTICS.COPILOT.SCHEMA_CARDS")
    return [{"table": r[0], "summary": r[1].split("\n")[0][:120]} for r in rows]


@mcp.tool()
def describe_table(table_name: str) -> str:
    """Full schema card for one table (columns, semantics, sample questions)."""
    return _describe_table_impl(table_name, _sf())


@mcp.tool()
def search_glossary(question: str, k: int = 5) -> list[str]:
    """Business-glossary terms most relevant to the question."""
    from copilot.retrieval import retrieve

    return retrieve(question, _sf(), k_cards=0, k_terms=k).glossary


if __name__ == "__main__":
    mcp.run()
```

Note: `FakeSnowflake.run_query` must serve the `SCHEMA_CARDS WHERE UPPER(table_name)` query — it already returns card rows for any sql containing "SCHEMA_CARDS" (verify; its return shape `(["TABLE_NAME","CARD"], [(name, card), ...])` means `_describe_table_impl` gets `rows[0][0]` = a table name, not a card — adjust `_describe_table_impl` test expectation OR make the impl select rows[0][-1]. Resolution: keep impl as written (real query returns one `card` column; `rows[0][0]` is the card) and in the TEST use a purpose-built fake:

```python
class CardFake(FakeSnowflake):
    def run_query(self, sql, params=()):
        if "WHERE UPPER(table_name)" in sql:
            return (["CARD"], [("GOLD.DIM_MACHINE - one row per installed machine",)])
        return super().run_query(sql, params)
```

and call `_describe_table_impl("GOLD.DIM_MACHINE", CardFake())`.)

- [ ] **Step 4: PASS + lint scope** — add `../mcp_server` to Makefile lint + CI; `uv run pytest tests/test_mcp_server.py -v` and `make lint`.

- [ ] **Step 5: Commit** — `git add mcp_server backend/tests/test_mcp_server.py Makefile .github/workflows/ci.yml && git commit -m "feat: MCP tool server with layer-2 SQL validation"`

---

### Task 6: Sync MCP client + executor wiring (TDD)

**Files:**
- Create: `backend/src/copilot/mcp_client.py`
- Test: `backend/tests/test_mcp_client.py`, `backend/tests/test_mcp_stdio_roundtrip.py`

**Interfaces:**
- Consumes: MCP server (Task 5); `build_graph(..., executor=...)` (Task 4).
- Produces: `McpExecutor()` — context-managed singleton with `.run_query(sql: str) -> tuple[list[str], list[list]]` (matches the executor callable contract), `.close()`. Spawns `mcp_server/server.py` via stdio using `sys.executable`, drives the async MCP `ClientSession` on a background thread event loop. Raises `McpError(RuntimeError)` with the server's error message on tool failure (so the graph's execute-node repair path sees the real SQL error).

- [ ] **Step 1: Failing unit test (no subprocess)** — `backend/tests/test_mcp_client.py`:

```python
import json

from copilot.mcp_client import _parse_tool_result


class Block:
    def __init__(self, text):
        self.type = "text"
        self.text = text


class ToolResult:
    def __init__(self, payload, is_error=False):
        self.content = [Block(json.dumps(payload) if isinstance(payload, dict) else payload)]
        self.isError = is_error
        self.structuredContent = payload if isinstance(payload, dict) else None


def test_parse_structured_content():
    cols, rows = _parse_tool_result(ToolResult({"columns": ["A"], "rows": [[1]]}))
    assert cols == ["A"] and rows == [[1]]


def test_parse_error_raises_with_message():
    import pytest

    from copilot.mcp_client import McpError
    with pytest.raises(McpError, match="invalid identifier"):
        _parse_tool_result(ToolResult("SQL compilation error: invalid identifier", True))
```

- [ ] **Step 2: FAIL**, then **Step 3: Implement `backend/src/copilot/mcp_client.py`**

```python
"""Sync wrapper over the async MCP stdio client, for use inside the agent graph."""
import asyncio
import json
import sys
import threading
from pathlib import Path

from copilot.config import REPO_ROOT


class McpError(RuntimeError):
    pass


def _parse_tool_result(result) -> tuple[list, list]:
    if getattr(result, "isError", False):
        text = result.content[0].text if result.content else "MCP tool error"
        raise McpError(text)
    payload = getattr(result, "structuredContent", None)
    if payload is None:
        payload = json.loads(result.content[0].text)
    if "result" in payload and "columns" not in payload:  # FastMCP wraps plain returns
        payload = payload["result"]
    return list(payload["columns"]), [list(r) for r in payload["rows"]]


class McpExecutor:
    """Owns a background event loop + stdio MCP session. One per process."""

    def __init__(self):
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._thread.start()
        self._session = None
        self._cm = []
        self._run(self._start())

    def _run(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout=60)

    async def _start(self):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        params = StdioServerParameters(
            command=sys.executable,
            args=[str(REPO_ROOT / "mcp_server" / "server.py")])
        transport = stdio_client(params)
        read, write = await transport.__aenter__()
        session_cm = ClientSession(read, write)
        session = await session_cm.__aenter__()
        await session.initialize()
        self._cm = [transport, session_cm]
        self._session = session

    def run_query(self, sql: str) -> tuple[list, list]:
        async def call():
            return await self._session.call_tool("run_query", {"sql": sql})

        return _parse_tool_result(self._run(call()))

    def close(self):
        async def stop():
            for cm in reversed(self._cm):
                await cm.__aexit__(None, None, None)

        try:
            self._run(stop())
        finally:
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(timeout=5)
```

- [ ] **Step 4: Stdio round-trip test (spawns real subprocess, no network — NOT live-marked but slow ~2s)** — `backend/tests/test_mcp_stdio_roundtrip.py`:

```python
"""Round-trip through a real stdio MCP server subprocess with Snowflake faked out.

The server subprocess imports copilot.snowflake_client for real, so we point it at a
stub via env: COPILOT_FAKE_SNOWFLAKE=1 makes server.py use an in-process fake.
"""
import pytest

from copilot.mcp_client import McpError, McpExecutor


@pytest.fixture(scope="module")
def executor(monkeypatch_module_env):
    ex = McpExecutor()
    yield ex
    ex.close()


@pytest.fixture(scope="module")
def monkeypatch_module_env():
    import os

    os.environ["COPILOT_FAKE_SNOWFLAKE"] = "1"
    yield
    os.environ.pop("COPILOT_FAKE_SNOWFLAKE", None)


def test_round_trip_query(executor):
    cols, rows = executor.run_query("SELECT model FROM GOLD.DIM_MACHINE")
    assert cols == ["MODEL"] and rows == [["TrueBeam"], ["Halcyon"]]


def test_round_trip_guard_rejection(executor):
    with pytest.raises(McpError, match="rejected by SQL guard"):
        executor.run_query("DROP TABLE GOLD.DIM_MACHINE")
```

Support this in `mcp_server/server.py` `_sf()`:

```python
def _sf():
    import os

    if os.environ.get("COPILOT_FAKE_SNOWFLAKE") == "1":
        class _Fake:
            def run_query(self, sql, params=()):
                if "SCHEMA_CARDS" in sql or "GLOSSARY" in sql:
                    return (["CARD"], [("stub card",)])
                return (["MODEL"], [("TrueBeam",), ("Halcyon",)])

        return _Fake()
    from copilot.snowflake_client import SnowflakeClient

    return SnowflakeClient(role="COPILOT_APP_RO")
```

- [ ] **Step 5: PASS** — `uv run pytest tests/test_mcp_client.py tests/test_mcp_stdio_roundtrip.py -v`. If the MCP SDK's actual result shape differs from `_parse_tool_result`'s assumptions (SDK versions vary), fix the PARSER, print the raw result in the report, and keep both tests green — never skip the round-trip test.

- [ ] **Step 6: Commit** — `git add backend/src/copilot/mcp_client.py backend/tests/test_mcp_client.py backend/tests/test_mcp_stdio_roundtrip.py mcp_server/server.py && git commit -m "feat: sync MCP client executor with stdio round-trip test"`

---

### Task 7: API integration — auth endpoints, role-scoped sessions, request log, feedback

**Files:**
- Create: `backend/src/copilot/request_log.py`
- Modify: `backend/src/copilot/api/main.py`
- Test: `backend/tests/test_request_log.py`; rewrite `backend/tests/test_api.py`

**Interfaces:**
- Consumes: `auth.*` (Task 2), `answer_question(..., conversation_id, executor)` (Task 4), `McpExecutor` (Task 6).
- Produces: `POST /auth/login {email, password} -> {token, role, email}` (401 on bad creds); `POST /chat` now REQUIRES Bearer token, uses `sf_ro` for analyst / `sf_admin` for admin, passes `conversation_id`, logs to REQUEST_LOG via writer client; `POST /feedback {request_id, conversation_id, rating: "up"|"down", comment} -> {"status":"recorded"}` (auth required); `request_log.log_request(sf_writer, *, request_id, conversation_id, user_role, question, response: ChatResponse, e2e_ms: int) -> None` — never raises.

- [ ] **Step 1: Failing tests** — `backend/tests/test_request_log.py`:

```python
from copilot.agent.pipeline import ChatResponse
from copilot.request_log import log_request
from tests.conftest import FakeSnowflake


def test_log_request_inserts_row():
    sf = FakeSnowflake()
    r = ChatResponse(answer="ok", intent="data_query", request_id="rid1",
                     sql="SELECT 1", tokens_in=10, tokens_out=5, retrieval_ms=99)
    log_request(sf, request_id="rid1", conversation_id="c1", user_role="analyst",
                question="q?", response=r, e2e_ms=1234)
    assert any("INSERT INTO MEDTECH_ANALYTICS.COPILOT.REQUEST_LOG" in q
               for q in sf.queries)


def test_log_request_never_raises():
    class Dead:
        def run_query(self, sql, params=()):
            raise RuntimeError("writer down")

    r = ChatResponse(answer="ok", request_id="rid2")
    log_request(Dead(), request_id="rid2", conversation_id=None, user_role="analyst",
                question="q?", response=r, e2e_ms=1)  # must not raise
```

Rewrite `backend/tests/test_api.py`:

```python
import bcrypt
from fastapi.testclient import TestClient

from copilot import auth
from copilot.api.main import app
from tests.conftest import FakeProvider, FakeSnowflake


def _client(monkeypatch):
    h = bcrypt.hashpw(b"pw123", bcrypt.gensalt()).decode()
    s = auth.get_settings()
    monkeypatch.setattr(s, "demo_analyst_password_hash", h)
    monkeypatch.setattr(s, "demo_admin_password_hash", h)
    monkeypatch.setattr(s, "jwt_secret", "test-secret")
    app.state.provider = FakeProvider()
    app.state.sf_ro = FakeSnowflake()
    app.state.sf_admin = FakeSnowflake()
    app.state.sf_writer = FakeSnowflake()
    app.state.executor = None
    return TestClient(app)


def _token(client, email="analyst@demo"):
    r = client.post("/auth/login", json={"email": email, "password": "pw123"})
    assert r.status_code == 200
    return r.json()


def test_login_success_and_failure(monkeypatch):
    c = _client(monkeypatch)
    body = _token(c, "admin@demo")
    assert body["role"] == "admin" and body["token"]
    assert c.post("/auth/login", json={"email": "admin@demo", "password": "no"}).status_code == 401


def test_chat_requires_auth(monkeypatch):
    c = _client(monkeypatch)
    assert c.post("/chat", json={"question": "hi"}).status_code == 401


def test_chat_uses_role_scoped_session_and_logs(monkeypatch):
    c = _client(monkeypatch)
    tok = _token(c)["token"]
    r = c.post("/chat", json={"question": "Which models?"},
               headers={"authorization": f"Bearer {tok}"})
    assert r.status_code == 200
    assert r.json()["request_id"]
    assert app.state.sf_ro.queries  # analyst hit the RO session
    assert not app.state.sf_admin.queries
    assert any("REQUEST_LOG" in q for q in app.state.sf_writer.queries)


def test_admin_chat_uses_admin_session(monkeypatch):
    c = _client(monkeypatch)
    tok = _token(c, "admin@demo")["token"]
    c.post("/chat", json={"question": "Which models?"},
           headers={"authorization": f"Bearer {tok}"})
    assert app.state.sf_admin.queries


def test_feedback_recorded(monkeypatch):
    c = _client(monkeypatch)
    tok = _token(c)["token"]
    r = c.post("/feedback",
               json={"request_id": "rid", "conversation_id": "c1",
                     "rating": "down", "comment": "wrong number"},
               headers={"authorization": f"Bearer {tok}"})
    assert r.status_code == 200
    assert any("INSERT INTO MEDTECH_ANALYTICS.COPILOT.FEEDBACK" in q
               for q in app.state.sf_writer.queries)


def test_healthz_open():
    assert TestClient(app).get("/healthz").json() == {"status": "ok"}
```

- [ ] **Step 2: FAIL**, then **Step 3: Implement**

`backend/src/copilot/request_log.py`:

```python
"""Fire-and-forget REQUEST_LOG writes. Telemetry must never break a request."""
from copilot.agent.pipeline import ChatResponse
from copilot.agent.prompts import PROMPT_VERSION


def log_request(sf_writer, *, request_id: str, conversation_id: str | None,
                user_role: str, question: str, response: ChatResponse,
                e2e_ms: int) -> None:
    try:
        sf_writer.run_query(
            "INSERT INTO MEDTECH_ANALYTICS.COPILOT.REQUEST_LOG "
            "(request_id, conversation_id, user_role, question, intent, sql_text, "
            "status, error_type, e2e_ms, retrieval_ms, tokens_in, tokens_out, "
            "prompt_version) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (request_id, conversation_id, user_role, question[:1000],
             response.intent, response.sql, "error" if response.error_type else "ok",
             response.error_type, e2e_ms, response.retrieval_ms,
             response.tokens_in, response.tokens_out, PROMPT_VERSION))
    except Exception:  # noqa: BLE001 — logging is best-effort by design
        pass
```

`backend/src/copilot/api/main.py` — full replacement:

```python
import time

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from copilot import auth
from copilot.agent.pipeline import ChatResponse, answer_question
from copilot.request_log import log_request

app = FastAPI(title="Analytics Copilot")
app.add_middleware(
    CORSMiddleware, allow_origins=["http://localhost:5173"],
    allow_methods=["*"], allow_headers=["*"],
)


class LoginRequest(BaseModel):
    email: str
    password: str


class ChatRequest(BaseModel):
    question: str
    conversation_id: str | None = None


class FeedbackRequest(BaseModel):
    request_id: str
    conversation_id: str | None = None
    rating: str  # "up" | "down"
    comment: str | None = None


def _deps():
    if not hasattr(app.state, "provider"):
        from copilot.config import get_settings
        from copilot.llm.provider import AnthropicProvider
        from copilot.snowflake_client import SnowflakeClient

        app.state.provider = AnthropicProvider()
        app.state.sf_ro = SnowflakeClient(role="COPILOT_APP_RO")
        app.state.sf_admin = SnowflakeClient(role="COPILOT_ADMIN")
        app.state.sf_writer = SnowflakeClient(role="COPILOT_APP_WRITER")
        app.state.executor = None
        if get_settings().use_mcp:
            from copilot.mcp_client import McpExecutor

            app.state.executor = McpExecutor()
    return app.state


def _require_role(request: Request) -> str:
    return auth.require_role(request)


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@app.post("/auth/login")
def login(req: LoginRequest) -> dict:
    role = auth.authenticate(req.email, req.password)
    if role is None:
        raise HTTPException(status_code=401, detail="invalid credentials")
    return {"token": auth.create_token(role, req.email), "role": role,
            "email": req.email.strip().lower()}


@app.post("/chat")
def chat(req: ChatRequest, role: str = Depends(_require_role)) -> ChatResponse:
    if not req.question.strip():
        raise HTTPException(status_code=400, detail="question is empty")
    state = _deps()
    sf = state.sf_admin if role == "admin" else state.sf_ro
    executor = state.executor.run_query if state.executor else None
    start = time.monotonic()
    resp = answer_question(req.question, state.provider, sf,
                           conversation_id=req.conversation_id, executor=executor)
    log_request(state.sf_writer, request_id=resp.request_id or "",
                conversation_id=req.conversation_id, user_role=role,
                question=req.question, response=resp,
                e2e_ms=int((time.monotonic() - start) * 1000))
    return resp


@app.post("/feedback")
def feedback(req: FeedbackRequest, role: str = Depends(_require_role)) -> dict:
    if req.rating not in ("up", "down"):
        raise HTTPException(status_code=400, detail="rating must be up|down")
    state = _deps()
    try:
        state.sf_writer.run_query(
            "INSERT INTO MEDTECH_ANALYTICS.COPILOT.FEEDBACK "
            "(conversation_id, request_id, rating, comment, prompt_version) "
            "VALUES (%s, %s, %s, %s, %s)",
            (req.conversation_id, req.request_id, req.rating,
             (req.comment or "")[:2000], __import__("copilot.agent.prompts",
                                                    fromlist=["PROMPT_VERSION"]).PROMPT_VERSION))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=503, detail="feedback store unavailable") from e
    return {"status": "recorded"}
```

(Replace the `__import__` hack with a top-of-file `from copilot.agent.prompts import PROMPT_VERSION` — written inline here only to show the value used; the implementation MUST use the normal import.)

Note the `_deps()` test-override contract changed: tests set `app.state.provider/sf_ro/sf_admin/sf_writer/executor` before calling — `_deps()` only constructs real clients when `provider` is absent.

- [ ] **Step 4: PASS** — `uv run pytest tests/test_request_log.py tests/test_api.py -v`, then full suite + `make lint`.

- [ ] **Step 5: Commit** — `git add -A backend && git commit -m "feat: auth-gated chat with role-scoped sessions, request log, feedback API"`

---

### Task 8: Frontend — login, role badge, auth-aware API

**Files:**
- Create: `frontend/src/auth.ts`, `frontend/src/Login.tsx`
- Modify: `frontend/src/api.ts`, `frontend/src/types.ts`, `frontend/src/App.tsx`, `frontend/src/App.css`
- Test: `frontend/src/App.test.tsx` (extend)

**Interfaces:**
- Consumes: `/auth/login`, Bearer-authed `/chat`.
- Produces: `auth.ts`: `getAuth() -> {token, role, email} | null`, `setAuth(a)`, `clearAuth()` (localStorage key `copilot_auth`); `Login.tsx` posts to `/auth/login`, calls `onLogin`; `App.tsx` renders Login when unauthenticated, else chat with role badge + logout; `api.ts` attaches `Authorization` and throws `AuthExpiredError` on 401.

- [ ] **Step 1: `frontend/src/auth.ts`**

```ts
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
```

- [ ] **Step 2: types.ts additions**

```ts
export interface ChatResponse {
  // ...existing fields unchanged...
  intent: string | null;
  request_id: string | null;
}

export interface LoginResponse {
  token: string;
  role: "analyst" | "admin";
  email: string;
}
```

- [ ] **Step 3: api.ts** — replace with:

```ts
import type { ChatResponse, LoginResponse } from "./types";
import { clearAuth, getAuth } from "./auth";

const BASE = import.meta.env.VITE_API_URL ?? "http://localhost:8000";

export class AuthExpiredError extends Error {}

async function post<T>(path: string, body: unknown, authed = true): Promise<T> {
  const headers: Record<string, string> = { "content-type": "application/json" };
  if (authed) {
    const a = getAuth();
    if (a) headers["authorization"] = `Bearer ${a.token}`;
  }
  const res = await fetch(`${BASE}${path}`, {
    method: "POST",
    headers,
    body: JSON.stringify(body),
  });
  if (res.status === 401 && authed) {
    clearAuth();
    throw new AuthExpiredError("session expired");
  }
  if (!res.ok) throw new Error(`API ${res.status}`);
  return res.json();
}

export const login = (email: string, password: string) =>
  post<LoginResponse>("/auth/login", { email, password }, false);

export const sendChat = (question: string, conversationId: string | null) =>
  post<ChatResponse>("/chat", { question, conversation_id: conversationId });

export const sendFeedback = (requestId: string, conversationId: string | null,
                             rating: "up" | "down", comment?: string) =>
  post<{ status: string }>("/feedback", {
    request_id: requestId, conversation_id: conversationId, rating, comment,
  });
```

- [ ] **Step 4: `frontend/src/Login.tsx`**

```tsx
import { useState } from "react";
import { login } from "./api";
import { setAuth, type AuthState } from "./auth";

export default function Login({ onLogin }: { onLogin: (a: AuthState) => void }) {
  const [email, setEmail] = useState("analyst@demo");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      const res = await login(email, password);
      const a: AuthState = { token: res.token, role: res.role, email: res.email };
      setAuth(a);
      onLogin(a);
    } catch {
      setError("Invalid credentials");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="login-shell">
      <form onSubmit={submit} className="login-card">
        <h1>Analytics Copilot</h1>
        <p className="sub">Sign in to query the warehouse</p>
        <input value={email} onChange={(e) => setEmail(e.target.value)}
               placeholder="email" autoComplete="username" />
        <input type="password" value={password}
               onChange={(e) => setPassword(e.target.value)} placeholder="password"
               autoComplete="current-password" />
        {error && <div className="login-error">{error}</div>}
        <button disabled={busy || !password}>Sign in</button>
        <p className="hint-small">analyst@demo sees masked PII · admin@demo sees all</p>
      </form>
    </div>
  );
}
```

- [ ] **Step 5: App.tsx auth gate** — top of component:

```tsx
const [authState, setAuthState] = useState<AuthState | null>(getAuth());
if (!authState) return <Login onLogin={setAuthState} />;
```

Header gains a role badge + logout:

```tsx
<div className="header-right">
  <span className={`badge ${authState.role}`}>{authState.role}</span>
  <button className="linklike" onClick={() => { clearAuth(); setAuthState(null); }}>
    sign out
  </button>
</div>
```

`sendChat` calls pass `conversationId` (Task 9 wires it). Catch `AuthExpiredError` in `submit` → `setAuthState(null)`.

App.css additions:

```css
.login-shell { display:flex; align-items:center; justify-content:center; height:100vh; }
.login-card { display:flex; flex-direction:column; gap:10px; width:320px;
              background:#1a2233; border:1px solid #232b3d; border-radius:14px; padding:28px; }
.login-card h1 { margin:0; font-size:20px; }
.login-card .sub { margin:0 0 6px; color:#8b94a7; font-size:13px; }
.login-error { color:#ff8a80; font-size:13px; }
.hint-small { color:#8b94a7; font-size:11.5px; margin:6px 0 0; }
.header-right { display:flex; gap:10px; align-items:center; margin-left:auto; }
.badge { font-size:11px; font-weight:700; text-transform:uppercase; padding:2px 8px;
         border-radius:99px; background:#1d4ed8; }
.badge.admin { background:#b42318; }
.linklike { background:none; border:none; color:#8b94a7; cursor:pointer; padding:0;
            font-size:12px; }
header { display:flex; align-items:center; gap:12px; }
```

- [ ] **Step 6: Test + build** — extend `App.test.tsx`:

```tsx
test("unauthenticated users see the login form", () => {
  localStorage.clear();
  render(<App />);
  expect(screen.getByPlaceholderText("password")).toBeDefined();
});
```

Run: `cd frontend && npm test -- --run && npm run build` → both pass.

- [ ] **Step 7: Commit** — `git add frontend && git commit -m "feat: login page, role badge, auth-aware API client"`

---

### Task 9: Frontend — feedback UI, multi-turn conversation, polish

**Files:**
- Modify: `frontend/src/App.tsx`, `frontend/src/App.css`, `frontend/src/types.ts` (Message gains `feedback?: "up" | "down"`)

**Interfaces:**
- Consumes: `sendFeedback` (Task 8), `request_id` on ChatResponse.
- Produces: per-assistant-message 👍/👎 (with optional one-line comment prompt on 👎 via `window.prompt`), sends feedback once per message and shows the recorded state; a stable `conversationId` (uuid via `crypto.randomUUID()`) per page load passed to every `sendChat` so follow-ups work; assumptions rendered under the SQL panel when present; network-failure messages get `.err` styling (closes a Phase 1 deferred minor).

- [ ] **Step 1: Implement** — in `App.tsx`:

```tsx
const conversationId = useRef<string>(crypto.randomUUID());
// pass conversationId.current to sendChat(q, conversationId.current)

async function giveFeedback(i: number, rating: "up" | "down") {
  const m = messages[i];
  if (!m.data?.request_id || m.feedback) return;
  const comment = rating === "down"
    ? window.prompt("What was wrong? (optional)") ?? undefined
    : undefined;
  try {
    await sendFeedback(m.data.request_id, conversationId.current, rating, comment);
    setMessages((ms) => ms.map((msg, idx) =>
      idx === i ? { ...msg, feedback: rating } : msg));
  } catch {
    /* feedback is best-effort */
  }
}
```

Per assistant message with `data`:

```tsx
{m.data?.request_id && (
  <div className="fb">
    {m.feedback
      ? <span className="fb-done">feedback: {m.feedback === "up" ? "👍" : "👎"}</span>
      : <>
          <button onClick={() => giveFeedback(i, "up")}>👍</button>
          <button onClick={() => giveFeedback(i, "down")}>👎</button>
        </>}
  </div>
)}
{m.data && m.data.assumptions.length > 0 && (
  <details><summary>Assumptions</summary>
    <ul>{m.data.assumptions.map((a, ai) => <li key={ai}>{a}</li>)}</ul>
  </details>
)}
```

Network-failure catch block: push `{ role: "assistant", text: ..., data: { ...empty, error_type: "network" } as ChatResponse }`? NO — keep Message shape honest: add `error?: boolean` to `Message`, set it in the catch, and render `className={... m.error ? "err" : ""}`.

CSS:

```css
.fb { margin-top: 8px; display: flex; gap: 6px; }
.fb button { background: #141b2b; border: 1px solid #232b3d; border-radius: 8px;
             padding: 2px 10px; cursor: pointer; font-size: 13px; }
.fb-done { color: #8b94a7; font-size: 12px; }
```

- [ ] **Step 2: Test + build** — `npm test -- --run && npm run build`.

- [ ] **Step 3: Commit** — `git add frontend && git commit -m "feat: feedback UI, stable conversation id, assumptions panel"`

---

### Task 10: Live verification + docs + tag

**Files:**
- Modify: `README.md` (quickstart: demo users step, MCP note, Claude Desktop config snippet)
- Modify: `backend/tests/live/test_slice_live.py` (add multi-turn + MCP-path live test)

**Interfaces:**
- Consumes: everything. Requires `.env` complete including the three auth lines (USER ACTION from Task 1).

- [ ] **Step 1: Add live tests** — append to `backend/tests/live/test_slice_live.py`:

```python
def test_multi_turn_followup_live():
    from copilot.llm.provider import AnthropicProvider
    from copilot.snowflake_client import SnowflakeClient

    provider = AnthropicProvider()
    sf = SnowflakeClient(role="COPILOT_APP_RO")
    r1 = answer_question("How many machines do we have per model?", provider, sf,
                         conversation_id="live-mt-1")
    assert r1.error_type is None and r1.intent == "data_query"
    r2 = answer_question("now only the Active ones", provider, sf,
                         conversation_id="live-mt-1")
    assert r2.error_type is None
    assert "STATUS" in (r2.sql or "").upper() or "ACTIVE" in (r2.sql or "").upper()


def test_mcp_executor_live():
    from copilot.mcp_client import McpExecutor

    ex = McpExecutor()
    try:
        cols, rows = ex.run_query(
            "SELECT COUNT(*) AS n FROM GOLD.FACT_MACHINE_UTILIZATION")
        assert rows[0][0] == 146000
    finally:
        ex.close()
```

- [ ] **Step 2: Run the full live gauntlet**

```bash
export PATH="$HOME/.local/bin:$PATH"
make lint && make test          # all unit suites green
make test-live                  # 3 live tests pass
```

Then manual end-to-end: `make api` + `make web`, sign in as analyst@demo (ask a question, thumbs-down it with a comment), sign in as admin@demo in a second browser profile and run `SELECT contact_email...` type question ("list treatment centers with their contact emails") — analyst sees `***MASKED***`, admin sees real addresses. Verify rows landed: REQUEST_LOG + FEEDBACK counts increased (via `SnowflakeClient(role="COPILOT_ADMIN")` count queries). Record actual outputs in the task report.

- [ ] **Step 3: README** — update quickstart: add step "5. Demo users: `uv run python ../scripts/gen_demo_users.py` → paste into .env"; move Phase-2 features from the "Phases 2-3 add..." sentence into the Phase-1-present-tense list (LangGraph orchestration, MCP tool server, JWT RBAC now real; remaining future: eval harness, admin console, AWS deployment); add a "Connect Claude Desktop to the MCP server" snippet:

```json
{"mcpServers": {"analytics-warehouse": {
  "command": "<repo>/backend/.venv/bin/python",
  "args": ["<repo>/mcp_server/server.py"]}}}
```

- [ ] **Step 4: Commit + tag**

```bash
git add -A && git commit -m "feat: phase 2 live verification, README, Claude Desktop MCP config"
git tag v0.2-agent
```

---

## Self-Review (done during planning)

- **Spec coverage (Phase 2 scope):** LangGraph StateGraph with plan/retrieve/generate/validate/execute/summarize + repair ≤1 + checkpointer memory ✓ (T4); intent routing incl. smalltalk/unsupported scope message and glossary path ✓ (T3/T4); MCP server 4 tools + layer-2 validation + side-effect denylist per threat-model note ✓ (T5); agent executes through MCP client ✓ (T6); JWT auth, two demo accounts, bcrypt, role dependency ✓ (T2/T7); analyst→RO masked / admin→ADMIN unmasked sessions ✓ (T7); REQUEST_LOG writes with prompt_version ✓ (T7); feedback endpoint + UI ✓ (T7/T9); login page + role routing/badge ✓ (T8); multi-turn conversation_id ✓ (T4/T9/T10); Claude Desktop demo bonus ✓ (T10). Deferred to Phase 3 by design: Admin Console UI, evals, CloudWatch, AWS/Terraform.
- **Placeholder scan:** the two inline "NO —" self-corrections in T5/T9 are explicit resolutions, not placeholders; `__import__` hack in T7 explicitly ordered replaced with a normal import. No TBDs.
- **Type consistency:** executor contract `(sql) -> (columns, rows)` matches `McpExecutor.run_query` and `sf.run_query` ✓; `ChatResponse` additive fields mirrored in types.ts ✓; `answer_question` keyword-only params consistent across T4/T7/T10 ✓; `app.state` keys (provider, sf_ro, sf_admin, sf_writer, executor) consistent between `_deps()` and tests ✓.
- **Known risks accepted:** MCP SDK result-shape variance (T6 Step 5 instructs fixing the parser against reality); LangGraph checkpointer state-key semantics (T4's input dict explicitly resets per-turn keys, history persists via checkpointer); `InMemorySaver` is per-process (fine for demo; "Redis in prod" is the interview answer).
```
