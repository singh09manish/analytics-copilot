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

## Deploying to AWS

The one-command path from an empty AWS account to a live URL:

```bash
cp infra/terraform.tfvars.example infra/terraform.tfvars   # edit github_repo first
make aws-up                                                 # terraform apply; prints app_url
make aws-secret                                              # pushes .env + the Snowflake key into Secrets Manager
git push                                                      # deploy.yml builds the image and rolls the service
```

`make aws-down` tears the whole stack back down (`terraform destroy`, buckets and the
ECR repo are `force_destroy`d so nothing stalls on leftover objects).

### Architecture decisions

- **MCP server runs inside the backend container, over stdio — not as a sidecar.** The
  spec sketched a sidecar over localhost HTTP. stdio is what the client is tested
  against, and adding an HTTP transport days before a demo is new, untested code on the
  critical path. It is still a real MCP server over a real transport; a sidecar is the
  answer to "how would you scale this?" — independent scaling, language independence,
  a network boundary you can authenticate — not a requirement to ship one.
- **Terraform state is local, not in S3/DynamoDB.** A remote backend is the right answer
  for a team, but provisioning it is a second bootstrap problem for a single operator.
  State files are gitignored (`infra/terraform.tfvars`, `*.tfstate*`), which means
  **teardown must happen from the same machine that ran `terraform apply`** — there is
  no shared state for another machine or CI to destroy from.
- **Fargate tasks run in public subnets with public IPs.** The textbook layout is
  private subnets behind a NAT gateway, but a NAT gateway runs ~$32/month — more than
  the rest of this stack combined — and the task only needs outbound egress to
  Snowflake and the Anthropic API. Inbound traffic is still restricted to the ALB's
  security group, so the public IP is not itself an entry point. A deliberate cost
  trade-off, stated rather than hidden.
- **One CloudFront distribution fronts both the SPA and the API.** The default
  behaviour serves the SPA from a private S3 bucket via Origin Access Control; `/api/*`
  forwards to the ALB. This gives HTTPS everywhere without owning a domain and
  collapses the app to a single origin, so CORS is moot in production.
- **Secrets never touch Terraform state.** Terraform creates the Secrets Manager
  container but not its contents; `scripts/aws_bootstrap_secret.py` pushes the real
  values from `.env` directly via the AWS CLI.

### Cost

Roughly **$1-2/day** while the stack sits idle between demos:

| Resource | ~cost/day |
|---|---|
| Fargate task (0.5 vCPU / 2GB, always on) | ~$0.90 |
| Application Load Balancer | ~$0.55 |
| CloudFront, S3, ECR, Secrets Manager, CloudWatch logs (7-day retention) | ~$0.10-0.30 |
| NAT gateway | $0 (not used — see above) |

Run `make aws-down` between demos if the cost matters more than the ~5-15 minutes it
takes CloudFront to redeploy on the next `make aws-up`.

## Connect Claude Desktop to the MCP server

The same MCP tool server the agent uses can be attached directly to Claude
Desktop for ad hoc exploration of the warehouse (note layer 2's caveat above —
a Desktop client can ask it to run as `COPILOT_ADMIN`):

```json
{"mcpServers": {"analytics-warehouse": {
  "command": "<repo>/backend/.venv/bin/python",
  "args": ["<repo>/mcp_server/server.py"]}}}
```
