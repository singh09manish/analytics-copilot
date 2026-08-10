# Analytics Copilot

Chat assistant that answers business questions against a governed Snowflake
medallion warehouse in natural language. Claude-powered vertical slice —
Cortex vector retrieval over a business glossary, schema-checked SQL
generation with a sqlglot safety guard, role-based PII masking via secure
views, FastAPI backend, React chat UI with a login page — now orchestrated by
a LangGraph agent (plan/retrieve/generate/validate/execute/summarize, with a
bounded repair loop and multi-turn conversation memory). Query execution
normally runs through an MCP tool server (`mcp_server/server.py`) that
re-validates SQL server-side as a second defense layer before it ever reaches
Snowflake; when `USE_MCP=false`, or if that subprocess fails to start or dies,
the app degrades to executing directly against the role-scoped Snowflake
session and logs the downgrade. Both the schema allowlist and the side-effect
denylist live in the app-side sqlglot guard (`sql_guard.py`, layer 1), so the
same rules apply either way — the MCP server re-runs them rather than owning
them. JWT auth backs two demo accounts (analyst/admin), each routed to its own
role-scoped Snowflake session
(`COPILOT_APP_RO` masked, `COPILOT_ADMIN` unmasked) — so the same question
returns masked or real PII depending on who's asking. Every request is
written to `COPILOT.REQUEST_LOG`, and every answer can be thumbs-up/down'd
with a comment into `COPILOT.FEEDBACK`. Still ahead: an eval harness, an
admin console, and AWS deployment (Terraform, ECS, CloudWatch).

## Documentation

- [docs/FLOW.md](docs/FLOW.md) — how the code executes, from entrypoint to Snowflake and
  back, with file and line references. Start here to find your way around.
- [docs/DECISIONS.md](docs/DECISIONS.md) — every meaningful choice and why, including the
  alternatives rejected and the bugs that forced a rethink.
- [docs/reviews/](docs/reviews/) — the Phase 2 security review findings and the fix record.
- [docs/PENDING-ACTIONS.md](docs/PENDING-ACTIONS.md) — anything waiting on a human.

## Quickstart
1. `cp .env.example .env` and fill in credentials
2. `make install && make check-env`
3. Seed + warehouse: `make seed`, run `warehouse/bootstrap.sql` in Snowsight (it
   sizes `COPILOT_WH`, sets its 60s `STATEMENT_TIMEOUT_IN_SECONDS`, and grants
   `COPILOT_APP_RO` read access to `GOLD` plus only `COPILOT.SCHEMA_CARDS` and
   `COPILOT.GLOSSARY`),
   `make load-bronze`, `make dbt-run dbt-test` (governance/masking is built into
   the dbt gold layer as secure views — see `warehouse/governance.sql` for the
   Enterprise-edition equivalent), `make ai-library`, then verify with
   `cd backend && uv run python ../scripts/verify_governance.py`
4. Demo users: `cd backend && uv run python ../scripts/gen_demo_users.py`,
   then paste the three printed lines (`DEMO_ANALYST_PASSWORD_HASH`,
   `DEMO_ADMIN_PASSWORD_HASH`, `JWT_SECRET`) into `.env`. The API refuses to
   start while `JWT_SECRET` is still the published default
   (`dev-secret-change-me`) or is shorter than 32 bytes — it's the whole
   authorization boundary between the analyst (masked) and admin (unmasked)
   Snowflake sessions.
5. Run: `make api` and `make web`

Architecture: see `docs/superpowers/specs/2026-08-08-analytics-copilot-design.md`.

## Status

The LangGraph agent, MCP tool server, JWT RBAC, request logging, and feedback
loop above are implemented and covered by the unit suite (`make test`), but
Phase 2 has not yet been live-verified end-to-end against Snowflake —
that verification is pending.

## Defense in depth, and what each layer is not

1. **sqlglot guard** (`backend/src/copilot/sql_guard.py`) — one statement, SELECT
   only, `GOLD` schema only, no side-effecting/metadata functions (`GET_DDL`,
   `SYSTEM$…`), row limit injected. The `COPILOT` schema is deliberately out of
   reach: it holds `REQUEST_LOG` and `FEEDBACK`. Retrieval reaches
   `COPILOT.SCHEMA_CARDS`/`GLOSSARY` with app-authored SQL that never passes
   through this guard.
2. **MCP tool server** (`mcp_server/server.py`) — re-runs layer 1 on whatever SQL
   it is handed. It is *not* an authentication boundary: it accepts
   `role="COPILOT_ADMIN"` from any stdio client with no credential check. That is
   not an escalation — anyone who can speak to it already holds the Snowflake
   private key — but it is only a re-validation layer, not an access control one.
   The API is what binds the role, from a verified JWT.
3. **Snowflake role grants** (`warehouse/bootstrap.sql`) — `COPILOT_APP_RO` gets
   `GOLD` plus exactly two `COPILOT` tables, so layer 3 does not depend on layer
   1's allowlist. `COPILOT_WH` carries `STATEMENT_TIMEOUT_IN_SECONDS = 60` so a
   runaway generated query cannot outlive the request that started it.

## Connect Claude Desktop to the MCP server

The same MCP tool server the agent uses can be attached directly to Claude
Desktop for ad hoc exploration of the warehouse (note layer 2's caveat above —
a Desktop client can ask it to run as `COPILOT_ADMIN`):

```json
{"mcpServers": {"analytics-warehouse": {
  "command": "<repo>/backend/.venv/bin/python",
  "args": ["<repo>/mcp_server/server.py"]}}}
```
