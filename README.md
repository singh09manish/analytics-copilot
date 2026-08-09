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
