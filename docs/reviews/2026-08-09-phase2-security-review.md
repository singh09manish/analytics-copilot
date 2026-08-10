# Phase 2 final whole-branch review — findings to fix

Branch `feat/phase2-agent` (2d97004..5ac8049). Every item below was verified by the reviewer
against the real code, most with executed probes. Fix all of them in one pass.

Baseline: backend `124 passed, 5 deselected`; frontend `18 passed`; ruff clean.

---

## CRITICAL

### C1 — An analyst can read every user's logged questions and every feedback comment; all three defense layers pass

`backend/src/copilot/sql_guard.py:5` · `mcp_server/server.py:107-114` · `warehouse/bootstrap.sql:34-35, 42-58`

`ALLOWED_SCHEMAS = {"GOLD", "COPILOT"}`. Layer 2 re-runs the same `validate`, so it inherits the
same allowance. Layer 3 grants `SELECT ON ALL/FUTURE TABLES IN SCHEMA ...COPILOT` to
`COPILOT_APP_RO` (bootstrap.sql:34-35), and Phase 2 is what put `REQUEST_LOG`, `FEEDBACK`, and
`EVAL_RESULTS` in that schema — created after those grants, so the FUTURE grant covers them.

Verified through both layers:

```
LAYER1 PASS : SELECT question, user_role, conversation_id FROM COPILOT.REQUEST_LOG
LAYER2 PASS : SELECT question, user_role, conversation_id FROM COPILOT.REQUEST_LOG
LAYER1 PASS : SELECT comment, rating FROM MEDTECH_ANALYTICS.COPILOT.FEEDBACK
LAYER2 PASS : SELECT comment, rating FROM MEDTECH_ANALYTICS.COPILOT.FEEDBACK
```

Failure scenario: `analyst@demo` asks "show me everything in MEDTECH_ANALYTICS.COPILOT.REQUEST_LOG".
The planner classifies it `data_query`, the LLM emits the obvious SELECT, the guard passes it, the
MCP server passes it, and `COPILOT_APP_RO` returns every user's raw question text, intent,
generated SQL, and role — including the admin's. `COPILOT.FEEDBACK` returns every free-text comment.
Harmless in Phase 1 (COPILOT held only SCHEMA_CARDS/GLOSSARY); not harmless now.

**Fix:** narrow `ALLOWED_SCHEMAS` to `{"GOLD"}`. The reviewer confirmed nothing in the LLM path
needs COPILOT: retrieval (`retrieval.py:37-55`) and the MCP metadata tools (`server.py:117-154`)
query COPILOT with app-authored SQL that never goes through `validate`; no schema card or prompt
mentions a COPILOT table; the two COPILOT strings in `test_sql_guard.py` (:56, :97) are negative
tests that still fail for their original reasons (`Into`, function-as-table).
Also tighten `warehouse/bootstrap.sql`: replace the schema-wide COPILOT RO grants with per-table
grants on `SCHEMA_CARDS` and `GLOSSARY` only (belt and braces — layer 3 should not depend on
layer 1's allowlist). Add a guard test asserting a COPILOT.REQUEST_LOG select is rejected.

### C2 — If the MCP subprocess dies after startup, every /chat blocks ~120s forever: no log, no restart, no fallback

`backend/src/copilot/mcp_client.py:93-99, 101-126, 153-154` · `agent/graph.py:180-184` ·
`api/main.py:101-116`

`_main` awaits `_stop_event` inside the `async with stdio_client(...)/ClientSession(...)`. If the
subprocess dies, that block raises (or anyio cancels the body → `CancelledError`, swallowed by
`_run_loop`'s `except asyncio.CancelledError: pass`). Either way `_main` returns, the thread exits,
and the loop is left **stopped but not closed** — `close()` is never called, `_closed` stays False,
and `app.state.executor` keeps the corpse for the process lifetime. Measured:

```
thread alive: False   loop running: False   loop closed: False
after 3.0s -> TimeoutError        # run_coroutine_threadsafe(...).result(timeout=N) burns the full N
```

`_run`'s `.result(timeout=60)` blocks a threadpool thread for the whole 60s. `execute()` catches
the TimeoutError as an exec error, the repair edge (`graph.py:182`) fires a second `execute`, and
that blocks another 60s — **~120s per request, permanently**, surfacing as `error_type="snowflake"`.
`/healthz` is a sync `def` sharing the same threadpool, so with uvicorn's 40-thread default about
40 concurrent users take the whole API down including the liveness probe.

**Fix:** have `_main`'s failure path (and `_run_loop`'s `finally`) mark the executor dead — set
`_closed`/a `_broken` flag and close the loop — and make `run_query` raise `McpError` immediately
when the session is gone instead of waiting out the timeout. Then make the API recover: rebuild the
executor, or fall back to direct execution (only valid once I2 lands, otherwise the fallback is
less safe than what it replaces). Add tests: a killed subprocess produces a fast `McpError`, not a
60s stall, and leaves no live thread.

---

## IMPORTANT

### I2 — Layer 2's side-effect denylist has no layer-1 equivalent, and any MCP hiccup silently deletes it

`sql_guard.py` (no GET_DDL/SYSTEM$ check) · `mcp_server/server.py:85-104` · `api/main.py:101-116` ·
`config.py:24`

```
LAYER1 PASS : SELECT GET_DDL('view', 'GOLD.DIM_TREATMENT_CENTER') AS d
LAYER2 BLOCK: ... side-effecting or metadata functions are not allowed
LAYER1 PASS : SELECT SYSTEM$CANCEL_ALL_QUERIES() AS x
LAYER2 BLOCK: ...
```

`_deps()` catches any `McpExecutor()` failure, logs one warning, sets `executor = None`, and caches
that decision on `app.state` forever; `USE_MCP` is also operator-flippable. In either state the
denylist is not in the path, and the graph falls through to `sf.run_query` where `sf` is `sf_admin`
for admin users (which holds `GRANT ALL ON DATABASE`). `SELECT SYSTEM$WAIT(600,'SECONDS')` is
available to any role and would pin a warehouse slot. The layer-3 backstop is explicitly unverified
and, with the Snowflake account locked, unverifiable.

**Fix:** move the side-effect check into layer 1 — call the AST-based `_deny_side_effects` logic
from `sql_guard.validate` (share the implementation; do not duplicate the code) so it holds
regardless of executor, and keep the MCP-side call for defense in depth. Add guard tests for the
obfuscated forms (`GET_DDL/*x*/(...)`, `"GET_DDL"(...)`, `SYSTEM$...`) at layer 1.

### I3 — auth.py:52: validly-signed token missing `role` returns 500, not 401 (CONFIRMED still present)

```
FEEDBACK roleless-token status: 500
CHAT     roleless-token status: 401   # _require_identity catches KeyError; require_role doesn't
```

`decode_token(...)["role"]` under `except AuthError` only, while its sibling
`main.py:_require_identity:140` already does `except (auth.AuthError, KeyError)`. One line:
`except (AuthError, KeyError)`. Add the covering test.

### I4 — The process-global checkpointer retains warehouse result rows forever, unbounded

`agent/graph.py:195-197`. Measured on this branch with a 1000-row result:

```
turn 1: checkpoints=9  saver~0.08 MB
turn 5: checkpoints=45 saver~0.44 MB    # ~90 KB retained per turn, never pruned
20 more conversations: saver~2.09 MB
```

`InMemorySaver` keeps every checkpoint (~9 per turn, one per super-step), each carrying a full copy
of the channel values including `rows` (up to 1000) and `context`. Nothing evicts, expires, or caps.
An authenticated user looping distinct `conversation_id`s with wide queries grows RSS without bound.
Governance angle: an **admin's unmasked PII rows sit in process memory for the process lifetime**,
which undercuts the masked/unmasked story the demo is built on.

**Fix:** bound it — an LRU/TTL wrapper over `InMemorySaver` capping tracked threads (and ideally
dropping `rows` from persisted state, since only `history` needs to survive a turn). Add a test that
memory does not grow without bound across many conversations.

### I5 — Abandoned MCP calls are never cancelled, and there is no Snowflake statement timeout

`mcp_client.py:154` · `warehouse/bootstrap.sql:4-5`. `.result(timeout=60)` raises but does not
cancel the `call_tool` coroutine; the MCP request stays outstanding and the query keeps running.
`COPILOT_WH` has no `STATEMENT_TIMEOUT_IN_SECONDS`, so the account default (172,800s) applies — a
runaway LLM-generated query burns credits for up to two days after the user was told it failed, and
the repair loop immediately launches a second one.

**Fix:** `ALTER WAREHOUSE COPILOT_WH SET STATEMENT_TIMEOUT_IN_SECONDS = 60;` in bootstrap.sql (and
mention it in the README warehouse section), and cancel the future on timeout in `_run`.

### I6 — REQUEST_LOG can't attribute a row to a user, and its conversation_id disagrees with the checkpointer's

`api/main.py:195` vs `:199-201`. The thread key is `f"{email}:{conversation_id}"` but the log writes
the raw client value and records only `user_role`. Two users who both send `conversation_id: "1"`
get correctly isolated agent memory and conflated audit rows, with no way to tell them apart. Same
for FEEDBACK. For a project whose thesis is governed access to sensitive data, an audit log that
can't name the actor is a real gap.

**Fix (stays inside the FIXED ops-table DDL):** log the scoped `f"{email}:{conversation_id}"` in both
`/chat` and `/feedback`.

### I7 — Telemetry failures are completely silent, and the hermetic tests can't catch a live INSERT break

`request_log.py:19-20` · `tests/conftest.py:34` · `tests/test_request_log.py:5-14`. Bare
`except Exception: pass` with no log line. `FakeSnowflake.run_query` accepts `params` and ignores it;
`test_log_request_inserts_row` asserts only that the SQL contains `INSERT INTO ... REQUEST_LOG`. A
wrong param count, missing grant, or VARCHAR overflow produces zero signal anywhere, while the
README claims "Every request is written to COPILOT.REQUEST_LOG". The counts are correct today
(13/13/13 and 5/5/5, hand-verified) — nothing defends them.

**Fix:** `logger.warning(..., exc_info=True)` in the handler (still never raise), plus a hermetic
assertion that `sql.count("%s") == len(params) == len(column_list)` for both INSERTs.

---

## MINOR (all cheap — include them)

- **M1** `retrieval.py:14, 29, 51` — `RetrievedContext.mode` is computed and read by nothing, and the
  Cortex→keyword fallback is a bare unlogged `except`. If Cortex is unavailable live, the app
  silently serves keyword-ranked context and the README's "Cortex vector retrieval" claim can't be
  falsified from any log. Log the fallback; surface `mode` in `ChatResponse` and REQUEST_LOG if it
  fits without breaking the additive-fields rule (adding one more optional field is fine).
- **M2** `request_log.py:1` — docstring says "Fire-and-forget" but `main.py:199` calls it
  synchronously on the `/chat` critical path. Fix the docstring (or make it background).
- **M4** `pipeline.py:64-70, 72-81` — the final `ChatResponse(...)` constructions sit outside the
  `try`. Pydantic's `ValidationError` is a `ValueError` and would escape the never-raises contract.
  Cannot fire today, but wrap it so the contract is structural rather than incidental.
- **M6** `api/main.py:29` — startup rejects only the exact literal `dev-secret-change-me`;
  `JWT_SECRET=x` sails through (the test run even emits `InsecureKeyLengthWarning: The HMAC key is
  11 bytes long`). Add a `len(secret) < 32` check.
- **M8** README paragraph 1 says MCP execution happens unconditionally; false when `USE_MCP=false`
  or the fallback fired. Make it match the Status section's honesty.
- **M7** README should note the MCP server accepts `role="COPILOT_ADMIN"` from any stdio client with
  no authentication — not an escalation (that caller already holds the Snowflake private key), but
  "defense layer 2" should not read as an auth boundary.
- **M3** `graph.py:195-197` — `InMemorySaver` import and `_CHECKPOINTER` assignment sit at the bottom
  of the file, after the function that references them. Move to the top.
- **M9** `snowflake_client.py:29-31` — check-then-act on `self._conn is None`; two threadpool threads
  racing first-use each build a connection and one is leaked. Phase 2 made these clients
  process-global and concurrently used. Add a lock.
- **Makefile** — `UV := cd backend && uv` doesn't use `~/.local/bin/uv`, so `make lint` fails locally
  with `uv: command not found` (CI is fine because setup-uv puts it on PATH). Make it work locally.

---

## Verification required after the fix wave

- `cd backend && ~/.local/bin/uv run pytest -q -m "not live"` — all green, count >= 124
- `cd frontend && npm test -- --run` — 18 green
- `make lint` from repo root — must now work locally
- `pgrep -f mcp_server/server.py` — empty after the suite
- Do NOT run live Snowflake tests: the account is locked (390507).
