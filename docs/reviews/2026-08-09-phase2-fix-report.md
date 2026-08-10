# Phase 2 final review — fix wave report

Branch `feat/phase2-agent`, five commits on top of `5ac8049`:

| commit | scope |
| --- | --- |
| `d410d15` | C1, I2, I5 (warehouse half) — layer-1 security |
| `1e67001` | C2, I5 (client half), M9 — MCP resilience |
| `f3d354b` | I4, M3 — bounded checkpointer |
| `c279585` | I3, I6, I7, M1, M2, M4, M6 — telemetry / auth |
| `08e6115` | M7, M8, Makefile — docs and tooling |
| `3d884d5` | follow-up: COPY GRANTS position (regression from C1's second-order fix) |

Baseline was backend `124 passed`, frontend `18 passed`. Now backend **169 passed,
5 deselected**, frontend **18 passed**, `make lint` clean and working locally.

---

## Per-finding: what I changed, and the test that covers it

### C1 — analysts could read every user's logged questions and feedback comments

**Layer 1** (`backend/src/copilot/sql_guard.py`): `ALLOWED_SCHEMAS` narrowed from
`{"GOLD", "COPILOT"}` to `{"GOLD"}`, with a comment explaining exactly why COPILOT is
off-limits and why nothing in the LLM path needs it.

**Layer 3** (`warehouse/bootstrap.sql`): the schema-wide COPILOT grants were the real
hole — layer 3 should not depend on layer 1's allowlist to close it. Replaced
`GRANT SELECT ON ALL/FUTURE TABLES IN SCHEMA ...COPILOT` with `REVOKE`s of both plus
per-table `GRANT SELECT` on `SCHEMA_CARDS` and `GLOSSARY` only.

Two second-order problems that the naive version of this fix would have created, and
how they are handled:

- Per-table grants need the tables to exist, but `make ai-library` creates them.
  bootstrap now creates both `IF NOT EXISTS` (empty, same DDL) before granting, so
  the file is correct in either run order on a fresh account.
- `warehouse/load_ai_library.py` used `CREATE OR REPLACE TABLE`, which **drops
  grants**. Re-running `make ai-library` would silently have broken retrieval for
  every analyst. It now uses `CREATE OR REPLACE TABLE ... COPY GRANTS`.

**Tests:** `tests/test_sql_guard.py::test_copilot_schema_rejected` (8 parametrised
cases: bare and fully-qualified `REQUEST_LOG`, `FEEDBACK`, `EVAL_RESULTS`,
`SCHEMA_CARDS`, quoted identifiers, a JOIN that smuggles it in beside a legal GOLD
table, and a CTE body), plus `test_gold_is_the_only_allowed_schema`. The three
pre-existing COPILOT strings in that file still fail for their original reasons
(`Into`, function-as-table), as the review predicted.

### C2 — a dead MCP subprocess bricked every request, permanently

Probing the real SDK (mcp 2.0.0) showed the failure has **two distinct shapes**, and
the review's suggested fix only covers one. Both are handled:

1. **The background loop exits** (anyio task group unwinds). `_run_loop`'s new
   `finally` publishes `_dead` and clears `_session` *before* the thread ends, so a
   later call fails immediately instead of being submitted to a stopped loop. A call
   already in flight when the loop dies polls `_dead` (50 ms) rather than waiting on
   a future nothing will ever resolve.
2. **The loop stays parked in `_main` while the transport is gone.** Measured: after
   `SIGKILL`ing the subprocess, `thread alive: True, dead: False` — stdio_client's
   reader task ends *cleanly* on EOF, so it never cancels the sibling waiting on
   `_stop_event`, and the executor looks perfectly healthy forever. Here `call_tool`
   itself raises `Connection closed`. `run_query` now treats anything escaping
   `call_tool` as transport-level and sets `_broken`; tool-level failures are
   unaffected, because those arrive as a `CallToolResult` with `isError` and never
   reach that handler.

`is_broken()` exposes all three states (`_dead` / `_closed` / `_broken`).
`api/main.py::_live_executor` consults it on every `/chat`: it closes the corpse,
rebuilds with a 15 s readiness budget (shorter than cold start, because this happens
inside a live request), and falls back to `executor=None` — direct execution — if the
rebuild also fails. That fallback is only safe because I2 landed in the same wave.
A post-startup death is now also logged; previously it was completely silent.

`close()` and `_abort_startup()` tolerate an already-dead loop
(`call_soon_threadsafe` can raise `RuntimeError` on a closed loop).

**Tests:**
- `tests/test_mcp_stdio_roundtrip.py::test_killed_server_subprocess_fails_fast_and_reports_broken`
  — spawns a real server, `SIGKILL`s exactly the PID it spawned (diffed against the
  pre-existing set so the module-scoped fixture's server is untouched), then asserts
  the first post-death call raises `McpError` in < 10 s, `is_broken()` is true, the
  repair-cycle call is < 2 s, and `close()` leaves no live thread and a closed loop.
- `...::test_stopped_background_loop_fails_fast_and_leaves_no_thread` — the other
  shape, plus a leaked-thread assertion.
- `tests/test_mcp_client.py::test_transport_failure_marks_executor_broken` and
  `::test_tool_level_error_does_not_mark_executor_broken` — the distinction that keeps
  a guard rejection from throwing away a healthy session.
- `::test_run_query_on_a_dead_executor_raises_immediately` — < 1 s with no loop at all.
- `tests/test_api.py::test_chat_replaces_a_dead_mcp_executor`,
  `::test_chat_falls_back_to_direct_execution_when_the_rebuild_fails`,
  `::test_chat_keeps_a_healthy_mcp_executor`.

### I2 — side-effect denylist had no layer-1 equivalent

The AST-walking implementation moved into `sql_guard.py` as `_is_denied_function` /
`deny_side_effects`, and `validate()` calls it inline. `mcp_server/server.py`'s
`_deny_side_effects` is now a four-line wrapper that calls the **same shared helper**
and re-raises as `ValueError` — layer 2 keeps its defense-in-depth call (it
re-validates SQL from any stdio client, including ones that never saw layer 1) but no
longer owns a second copy that could drift.

**Tests:** `test_side_effecting_functions_rejected_at_layer1` (7 cases, including
`GET_DDL/*x*/(...)`, `GET_DDL -- x\n(...)`, `"GET_DDL"(...)`, `SYSTEM$WAIT` in a
`WHERE` clause), `test_string_literal_naming_a_denied_function_is_not_a_call` (guards
against over-blocking), `test_deny_side_effects_shares_layer1_implementation`. The
existing layer-2 tests still pass unchanged.

### I3 — roleless token → 500

`auth.require_role` now catches `(AuthError, KeyError)`. `/feedback` separately moved
to `_require_identity` (see I6), so the endpoint returns 401 by two independent routes.

**Tests:** `test_auth.py::test_require_role_rejects_a_validly_signed_token_with_no_role_claim`
(the helper itself), `test_api.py::test_feedback_token_without_role_claim_is_401_not_500`
(the endpoint), `test_api.py::test_require_role_dependency_maps_missing_claim_to_401`.

### I4 — unbounded checkpointer

New `backend/src/copilot/agent/checkpointer.py`: `BoundedInMemorySaver` caps **both**
growth axes, because the finding's measurements show both (45 checkpoints after 5
turns on one conversation; +2 MB over 20 new conversations):

- LRU over threads (64), evicted whole via `delete_thread` (storage + writes + blobs).
- 24 checkpoints per thread — comfortably above one turn's ~9 super-steps, so a turn
  in flight is never pruned out from under itself. Pruning recomputes the live blob
  set from the surviving checkpoints' `channel_versions`; without that it would drop
  the index and reclaim none of the row payloads, which is where the memory actually
  is.

Measured, same workload, 1000-row results, before → after:

```
256 conversations    6.00 MB -> 1.50 MB   (flat from the 64-thread steady state)
45 turns, one conv   1.06 MB -> 0.07 MB   (405 checkpoints -> 24)
```

I considered the review's "ideally drop `rows` from persisted state" and did not do
it: `rows` has to survive `execute → summarize → remember → END` because
`answer_question` reads it off the final state, so making it ephemeral would empty
every data answer. The cap achieves the same ceiling without that risk.

**Tests:** `tests/test_checkpointer.py` — five tests covering the thread LRU (and that
eviction reclaims blobs/writes, not just the index), the per-thread cap and its blob
pruning, and two end-to-end `answer_question` tests asserting retained bytes stay flat
across 256 conversations and across 45 turns of one conversation. Both end-to-end
assertions fail against a plain `InMemorySaver` (verified: 4× and 9× growth).

### I5 — abandoned MCP calls and no Snowflake statement timeout

- `mcp_client._run` cancels the future on timeout and raises `McpError` (previously
  the coroutine stayed outstanding with its Snowflake query still running).
- `warehouse/bootstrap.sql`: `ALTER WAREHOUSE COPILOT_WH SET
  STATEMENT_TIMEOUT_IN_SECONDS = 60;` (matching the client-side timeout), documented
  in the README warehouse step and the defense-in-depth section.

### I6 — audit rows could not name their actor

`api/main.py` gained two named helpers so the two notions of "conversation" are
explicit and can't drift again: `_scoped_conversation_id` (checkpointer thread key,
`None` stays `None`) and `_logged_conversation_id` (audit key, always
`f"{email}:{conversation_id or ''}"`, never `None`). Both `/chat` and `/feedback` log
the scoped value, which stays entirely inside the fixed ops-table DDL — no new
columns. `/feedback` switched to `_require_identity` to get the email.

**Tests:** `test_chat_logs_the_identity_scoped_conversation_id`,
`test_two_users_sharing_a_conversation_id_are_distinguishable_in_the_log`,
`test_chat_without_conversation_id_still_names_the_actor`,
`test_feedback_logs_the_identity_scoped_conversation_id`.

### I7 — silent telemetry failures, untestable INSERTs

`log_request`'s handler now emits `logger.warning(..., exc_info=True)` including the
`request_id`, and still never raises. Both INSERTs keep their column tuple beside the
statement (`request_log.COLUMNS/INSERT_SQL`, `main.FEEDBACK_COLUMNS/FEEDBACK_INSERT_SQL`)
and build the placeholder list from it, so a drift is impossible by construction *and*
assertable. `FakeSnowflake` now records `(sql, params)` pairs in `.calls` — it
previously accepted `params` and threw them away, which is what made the whole class
of bug invisible.

**Tests:** `test_request_log_insert_column_placeholder_and_param_counts_agree` and
`test_request_log_columns_match_the_fixed_ops_table_ddl` (13),
`test_api.py::test_feedback_insert_column_placeholder_and_param_counts_agree` (5),
`test_log_request_failure_is_logged_not_silent`.

### Minors

| # | change | test |
| --- | --- | --- |
| M1 | Cortex→keyword fallback logs a warning; `mode` reaches `ChatResponse.retrieval_mode` (additive optional) via `AgentState.retrieval_mode`, and is mirrored in `frontend/src/types.ts`. **Not** added to REQUEST_LOG — that column list is fixed. | `test_chat_surfaces_the_retrieval_mode` (asserts both the field and the log line) |
| M2 | `request_log` docstring no longer claims fire-and-forget; states it is synchronous on `/chat`'s critical path | — |
| M3 | `InMemorySaver` import and `_CHECKPOINTER` moved to the top of `graph.py` | `test_graph_uses_the_bounded_saver` |
| M4 | both final `ChatResponse(...)` constructions wrapped in the `try`, with a degraded response on `ValidationError` | contract is structural; existing never-raises tests still pass |
| M6 | startup also rejects `len(secret.encode()) < 32` | `test_app_refuses_to_start_with_a_short_jwt_secret`, `test_app_starts_with_a_long_enough_jwt_secret` |
| M7 | README "Defense in depth, and what each layer is not" — layer 2 accepts `role="COPILOT_ADMIN"` from any stdio client with no credential check | — |
| M8 | README paragraph 1 now says MCP execution is conditional and describes the degradation | — |
| M9 | `SnowflakeClient._connection` connects under a lock (double-checked) | covered by `test_deps_cold_start_is_race_free` |
| Makefile | `UV_BIN := $(shell command -v uv || echo $(HOME)/.local/bin/uv)` | verified by running `make lint` with `uv` off PATH |

Incidental: the suite's JWT test secrets were 11 bytes, which both tripped the new M6
check in the two lifespan tests and produced 35 `InsecureKeyLengthWarning`s per run.
They are now ≥ 32 bytes; the suite is warning-free.

---

## Test evidence

```
$ cd backend && ~/.local/bin/uv run pytest -q -m "not live"
169 passed, 5 deselected in 25.58s

$ cd frontend && npm test -- --run
 Test Files  2 passed (2)
      Tests  18 passed (18)

$ env PATH=/usr/bin:/bin:/usr/sbin:/sbin make lint     # uv deliberately off PATH
cd backend && /Users/manishsmac/.local/bin/uv run ruff check src tests ../data ../scripts ../warehouse ../mcp_server
All checks passed!

$ pgrep -f mcp_server/server.py
(none)

$ cd frontend && npm run build
✓ built in 86ms
```

C1/I2 re-probed through both layers, in the review's own format:

```
LAYER1 BLOCK: schema COPILOT is not allowed (use GOLD)             | LAYER2 BLOCK: rejected by SQL guard: schema COPILOT ...
   SELECT question, user_role, conversation_id FROM COPILOT.REQUEST_LOG
LAYER1 BLOCK: schema COPILOT is not allowed (use GOLD)             | LAYER2 BLOCK: rejected by SQL guard: schema COPILOT ...
   SELECT comment, rating FROM MEDTECH_ANALYTICS.COPILOT.FEEDBACK
LAYER1 BLOCK: side-effecting or metadata functions are not allowed | LAYER2 BLOCK: rejected by SQL guard: side-effecting ...
   SELECT GET_DDL('view', 'GOLD.DIM_TREATMENT_CENTER') AS d
LAYER1 BLOCK: side-effecting or metadata functions are not allowed | LAYER2 BLOCK: rejected by SQL guard: side-effecting ...
   SELECT GET_DDL/*x*/('TABLE','GOLD.DIM_MACHINE')
LAYER1 BLOCK: side-effecting or metadata functions are not allowed | LAYER2 BLOCK: rejected by SQL guard: side-effecting ...
   SELECT "GET_DDL"('TABLE','GOLD.DIM_MACHINE')
LAYER1 BLOCK: side-effecting or metadata functions are not allowed | LAYER2 BLOCK: rejected by SQL guard: side-effecting ...
   SELECT SYSTEM$CANCEL_ALL_QUERIES() AS x
LAYER1 BLOCK: side-effecting or metadata functions are not allowed | LAYER2 BLOCK: rejected by SQL guard: side-effecting ...
   SELECT SYSTEM$WAIT(600,'SECONDS') AS x
LAYER1 PASS                                                        | LAYER2 PASS
   SELECT model FROM GOLD.DIM_MACHINE
```

C2, before the fix (real subprocess, `SIGKILL`ed after a successful query):

```
is_broken: False   thread alive: True   dead: False
OTHER MCPError after 0.01s: Connection closed      # executor still looks healthy -> never replaced
```

after:

```
WARNING copilot.mcp_client: MCP transport is unusable; this executor is marked broken
McpError after 0.00s: MCP transport failed: Connection closed
```

No live Snowflake call was made, and `make test-live` was not run — the account is
locked (390507).

---

## Snowsight statements the user must re-run once the account is unlocked

Run as `ACCOUNTADMIN`. All are safe to re-run. Re-running the whole of
`warehouse/bootstrap.sql` also works; this is the minimal set that changed.

```sql
USE ROLE ACCOUNTADMIN;

-- I5: cap runaway generated queries (account default is 172800s)
ALTER WAREHOUSE COPILOT_WH SET STATEMENT_TIMEOUT_IN_SECONDS = 60;

-- C1: remove the schema-wide COPILOT read that exposed REQUEST_LOG / FEEDBACK /
-- EVAL_RESULTS to every analyst. These MUST run before the grants below.
REVOKE SELECT ON ALL TABLES IN SCHEMA MEDTECH_ANALYTICS.COPILOT FROM ROLE COPILOT_APP_RO;
REVOKE SELECT ON FUTURE TABLES IN SCHEMA MEDTECH_ANALYTICS.COPILOT FROM ROLE COPILOT_APP_RO;

-- C1: the two COPILOT tables the app legitimately reads. (No-ops for the CREATEs on
-- this account -- the tables already exist -- but keep them for a fresh bootstrap.)
CREATE TABLE IF NOT EXISTS MEDTECH_ANALYTICS.COPILOT.GLOSSARY (
  term VARCHAR, definition VARCHAR, related_tables VARCHAR, embedding VECTOR(FLOAT, 768)
);
CREATE TABLE IF NOT EXISTS MEDTECH_ANALYTICS.COPILOT.SCHEMA_CARDS (
  table_name VARCHAR, card VARCHAR, embedding VECTOR(FLOAT, 768)
);
GRANT SELECT ON TABLE MEDTECH_ANALYTICS.COPILOT.SCHEMA_CARDS TO ROLE COPILOT_APP_RO;
GRANT SELECT ON TABLE MEDTECH_ANALYTICS.COPILOT.GLOSSARY TO ROLE COPILOT_APP_RO;
```

Verification queries to run afterwards:

```sql
-- Expect SCHEMA_CARDS and GLOSSARY only (no REQUEST_LOG / FEEDBACK / EVAL_RESULTS):
SHOW GRANTS TO ROLE COPILOT_APP_RO;

-- Expect an insufficient-privileges error:
USE ROLE COPILOT_APP_RO;
SELECT * FROM MEDTECH_ANALYTICS.COPILOT.REQUEST_LOG LIMIT 1;

-- Expect rows:
SELECT COUNT(*) FROM MEDTECH_ANALYTICS.COPILOT.SCHEMA_CARDS;

-- Expect 60:
SHOW PARAMETERS LIKE 'STATEMENT_TIMEOUT_IN_SECONDS' IN WAREHOUSE COPILOT_WH;
```

**Also note for pending-actions:** `warehouse/load_ai_library.py` now issues
`CREATE OR REPLACE TABLE ... COPY GRANTS`. If `make ai-library` is ever run against an
account where the per-table grants have *not* yet been applied, `COPY GRANTS` has
nothing to copy and the grants must be re-issued afterwards. Order: bootstrap grants
first, then `make ai-library`.

---

## Deviations, and anything I could not fix

- **M1, REQUEST_LOG half — deliberately not done.** The finding says to surface
  `mode` in REQUEST_LOG "if it fits without breaking the additive-fields rule". It
  does not fit: the ops-table column lists are a hard constraint (REQUEST_LOG 14 cols
  as created in bootstrap.sql) and there is no spare column to fold it into without
  corrupting an existing one. It is surfaced in `ChatResponse` and in a log line
  instead. If it is wanted in the warehouse, that is a Phase 3 DDL change.
- **I4, "drop rows from persisted state" — deliberately not done**, for the reason in
  the I4 section above (it would empty every data answer). The caps deliver the same
  bound.
- **C2, the request that discovers the death still fails.** `_live_executor` runs
  before `answer_question`, so a death detected mid-request is repaired for
  *subsequent* requests; the current one degrades gracefully through the graph's
  normal error path (two fast failures, then the standard warehouse-retry message)
  rather than stalling. Retrying inside the graph would mean a rebuild (up to 15 s)
  inside a node, which I judged worse. Easy to revisit.
- **C2's rebuild rate is not limited.** If the MCP server is permanently unstartable,
  every `/chat` pays one 15 s rebuild attempt before falling back. Bounded and logged,
  but a cooldown would be better under sustained failure.
- **`main._require_role` was deleted** (it became unused when `/feedback` moved to
  `_require_identity`). `auth.require_role` is unchanged in shape, fixed per I3, and
  still directly covered by `test_auth.py` including via `Depends()`.
- **Four `# noqa: BLE001` comments removed** in touched code. Ruff exempts handlers
  that log with `exc_info` or re-raise from BLE001, so once those handlers started
  logging, `RUF100` flagged the directives as dead. Prose comments kept.
- **Nothing was verified against live Snowflake.** The bootstrap and load_ai_library
  changes are reasoned, not executed.

## Concerns

1. **The bootstrap DDL for `GLOSSARY`/`SCHEMA_CARDS` is now duplicated** between
   `warehouse/bootstrap.sql` and `warehouse/load_ai_library.py`. Both are commented to
   point at the other. It was the only way to make per-table grants issuable on a fresh
   account before the loader runs. If those column lists change, both must change.
2. **`COPY GRANTS` was written in the wrong position, and has been corrected.** My
   first version placed it *before* the column list
   (`CREATE OR REPLACE TABLE <name> COPY GRANTS (<cols>)`). That spelling is only
   legal in the CTAS variant (`... COPY GRANTS AS SELECT ...`); the column-definition
   form puts `COPY GRANTS` **after** the closing paren. As written it would have
   raised a compilation error on the first statement of `make ai-library`, making the
   retrieval corpus unreloadable — precisely the path a fresh-account rebuild takes.
   No hermetic test could have caught it and the account lock hid it. Caught in
   scoped re-review; fixed in `3d884d5`. Evidence (both statements extracted from the
   file as they will actually be sent, round-tripped through sqlglot's Snowflake
   dialect — byte-identical, and `COPY GRANTS` parses as a real `CopyGrantsProperty`
   rather than being swallowed):

   ```
   IN  : CREATE OR REPLACE TABLE MEDTECH_ANALYTICS.COPILOT.GLOSSARY (term VARCHAR,
         definition VARCHAR, related_tables VARCHAR, embedding VECTOR(FLOAT, 768)) COPY GRANTS
   OUT : CREATE OR REPLACE TABLE MEDTECH_ANALYTICS.COPILOT.GLOSSARY (term VARCHAR,
         definition VARCHAR, related_tables VARCHAR, embedding VECTOR(FLOAT, 768)) COPY GRANTS
   round-trips unchanged: True | copy_grants flag: True

   IN  : CREATE OR REPLACE TABLE MEDTECH_ANALYTICS.COPILOT.SCHEMA_CARDS (table_name VARCHAR,
         card VARCHAR, embedding VECTOR(FLOAT, 768)) COPY GRANTS
   OUT : CREATE OR REPLACE TABLE MEDTECH_ANALYTICS.COPILOT.SCHEMA_CARDS (table_name VARCHAR,
         card VARCHAR, embedding VECTOR(FLOAT, 768)) COPY GRANTS
   round-trips unchanged: True | copy_grants flag: True
   ```

   sqlglot parses *both* spellings but normalizes its output to the post-column
   position, which is itself corroboration of which one Snowflake's grammar accepts.
   Still unexecuted against Snowflake: worth running `make ai-library` once after the
   account unlocks, then re-checking `SHOW GRANTS TO ROLE COPILOT_APP_RO`.
3. **`REVOKE ... ON FUTURE TABLES` is the statement I would most want to see execute.**
   If it silently no-ops on this account, the future grant survives and any *new*
   COPILOT table would again be analyst-readable. Layer 1 still blocks it, but the
   whole point of C1's layer-3 half is not to depend on that. The `SHOW GRANTS` check
   above is what confirms it.
4. **The transport-death detection is keyed on "any exception escaping `call_tool`".**
   That is the right structural line (tool errors arrive as data, not exceptions), but
   it means a hypothetical transient SDK exception would retire an otherwise healthy
   executor. The cost is one rebuild, so I preferred the false positive to a false
   negative — a false negative is the C2 bug.
5. **The checkpointer caps (64 threads / 24 checkpoints) are demo-sized**, giving a
   ~1.5 MB ceiling with 1000-row results. A busier deployment would want them
   configurable rather than module constants.
6. **Evicting a thread silently drops that conversation's history** — the next turn
   starts fresh instead of erroring. That is the right behaviour for a demo, but it is
   a behaviour change worth knowing about if a reviewer tests a long-idle conversation
   while 64 others are active.
