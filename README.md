# Analytics Copilot

Chat assistant that answers business questions against a governed Snowflake
medallion warehouse in natural language. Phase 1 (this branch): Claude-powered
vertical slice — Cortex vector retrieval over a business glossary, schema-checked
SQL generation with a sqlglot safety guard, role-based PII masking via secure
views, FastAPI backend, React chat UI. Phases 2-3 add LangGraph multi-agent
orchestration, an MCP tool server, JWT RBAC + admin console, an eval harness,
and AWS deployment (Terraform, ECS, CloudWatch).

## Quickstart
1. `cp .env.example .env` and fill in credentials
2. `make install && make check-env`
3. Seed + warehouse: `make seed`, run `warehouse/bootstrap.sql` in Snowsight,
   `make load-bronze`, `make dbt-run dbt-test` (governance/masking is built into
   the dbt gold layer as secure views — see `warehouse/governance.sql` for the
   Enterprise-edition equivalent), `make ai-library`, then verify with
   `cd backend && uv run python ../scripts/verify_governance.py`
4. Run: `make api` and `make web`

Architecture: see `docs/superpowers/specs/2026-08-08-analytics-copilot-design.md`.
