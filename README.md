# Analytics Copilot

Chat assistant that answers business questions against a governed Snowflake
medallion warehouse in natural language. Claude-powered vertical slice —
Cortex vector retrieval over a business glossary, schema-checked SQL
generation with a sqlglot safety guard, role-based PII masking via secure
views, FastAPI backend, React chat UI with a login page — now orchestrated by
a LangGraph agent (plan/retrieve/generate/validate/execute/summarize, with a
bounded repair loop and multi-turn conversation memory). Query execution runs
through an MCP tool server (`mcp_server/server.py`) that re-validates SQL
server-side as a second defense layer (sqlglot guard + a side-effect
denylist) before it ever reaches Snowflake. JWT auth backs two demo accounts
(analyst/admin), each routed to its own role-scoped Snowflake session
(`COPILOT_APP_RO` masked, `COPILOT_ADMIN` unmasked) — so the same question
returns masked or real PII depending on who's asking. Every request is
written to `COPILOT.REQUEST_LOG`, and every answer can be thumbs-up/down'd
with a comment into `COPILOT.FEEDBACK`. Still ahead: an eval harness, an
admin console, and AWS deployment (Terraform, ECS, CloudWatch).

## Quickstart
1. `cp .env.example .env` and fill in credentials
2. `make install && make check-env`
3. Seed + warehouse: `make seed`, run `warehouse/bootstrap.sql` in Snowsight,
   `make load-bronze`, `make dbt-run dbt-test` (governance/masking is built into
   the dbt gold layer as secure views — see `warehouse/governance.sql` for the
   Enterprise-edition equivalent), `make ai-library`, then verify with
   `cd backend && uv run python ../scripts/verify_governance.py`
4. Demo users: `cd backend && uv run python ../scripts/gen_demo_users.py`,
   then paste the three printed lines (`DEMO_ANALYST_PASSWORD_HASH`,
   `DEMO_ADMIN_PASSWORD_HASH`, `JWT_SECRET`) into `.env`. The API refuses to
   start while `JWT_SECRET` is still the published default
   (`dev-secret-change-me`) — it's the whole authorization boundary between
   the analyst (masked) and admin (unmasked) Snowflake sessions.
5. Run: `make api` and `make web`

Architecture: see `docs/superpowers/specs/2026-08-08-analytics-copilot-design.md`.

## Status

The LangGraph agent, MCP tool server, JWT RBAC, request logging, and feedback
loop above are implemented and covered by the unit suite (`make test`), but
Phase 2 has not yet been live-verified end-to-end against Snowflake —
that verification is pending.

## Connect Claude Desktop to the MCP server

The same MCP tool server the agent uses can be attached directly to Claude
Desktop for ad hoc exploration of the warehouse:

```json
{"mcpServers": {"analytics-warehouse": {
  "command": "<repo>/backend/.venv/bin/python",
  "args": ["<repo>/mcp_server/server.py"]}}}
```
