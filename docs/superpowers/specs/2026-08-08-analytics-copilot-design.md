# Analytics Copilot — Design Spec

**Date:** 2026-08-08 (Saturday)
**Purpose:** A production-grade demo project for an AI/LLM engineering portfolio, built to a one-week deadline.
**Panel:** (details omitted from the public repo)
**Strategy:** Build-heavy (~70/30 build vs. prep). Demo posture: *mention + ready if asked* — weave the project into answers, live app + GitHub repo ready to screen-share.

## Goal

A chat assistant ("Analytics Copilot") that lets business users query a governed Snowflake warehouse in natural language. Every claim on the resume is implemented for real, right-sized: Claude + LangChain/LangGraph + RAG, Anthropic API pattern, MCP tool server, schema-checked structured extraction, multi-agent orchestration, governed medallion warehouse, vector store of business glossary + data contracts, eval harness with golden datasets, feedback loop, CloudWatch telemetry, CI/CD, Terraform on AWS.

**Non-goals:** self-service signup or SSO (Cognito/Entra is the "real prod" talking point — demo uses two fixed accounts), multi-tenant concerns, Power BI integration (talking point only), production-scale HA.

## Constraints & Assets

- 5 days until the interview (user available full days); build must stay demoable at every stage (vertical-slice-first).
- User has: Anthropic API key, personal AWS account, Snowflake account/trial.
- Run cost target ≈ $1–2/day; teardown script included.
- Repo name: `analytics-copilot`. UI title: "Analytics Copilot".

## Architecture (approved)

Monorepo, four deployable pieces:

1. **React SPA** (S3 + CloudFront): login page, then role-routed app — chat panel, transparent generated-SQL panel (syntax-highlighted), result table, 👍/👎 + comment per answer; admins additionally get the Admin Console (below). Minimal vitest smoke tests. Stretch: chart rendering from a chart-spec emitted by the summarize node.
2. **FastAPI backend** (ECS Fargate + ALB): `/auth/login`, `/chat`, `/feedback`, `/admin/*`, health endpoint. JWT role dependency on every endpoint. Runs the LangGraph agent. Typed error taxonomy (validation / Snowflake / LLM / retrieval-miss) → graceful chat messages, never stack traces.
3. **MCP server** (Python MCP SDK, same repo): tools `list_tables`, `describe_table`, `search_glossary`, `run_query` (read-only enforced server-side too). stdio transport locally; on AWS it runs as a sidecar container in the same Fargate task, reached over HTTP on localhost. Demo bonus: Claude Desktop can connect to the same server.
4. **Snowflake** — warehouse + AI Data Library (below).

### Data flow for one question

User → `/chat` → LangGraph `StateGraph` (state: messages, intent, retrieved context, SQL draft, validation result, query result, repair_count):

1. **Plan** (Claude, forced tool-use → Pydantic `QueryPlan`): intent = data_query | glossary_lookup | smalltalk | unsupported; entities. Smalltalk/unsupported → polite scope message; SQL never generated.
2. **Retrieve**: embed question → top-k schema cards + glossary terms via Snowflake Cortex vector search. Retrieval latency timed and emitted as a metric.
3. **Generate SQL** (Claude → Pydantic `SqlDraft {sql, tables_used, assumptions[]}`). Pydantic validation failure → one retry with the validation error appended.
4. **Validate** (sqlglot, deterministic): single SELECT only, GOLD/COPILOT schemas only, no DDL/DML, auto-`LIMIT 1000`.
5. **Execute** via MCP client → `run_query`, 30 s timeout.
6. **Summarize** (Claude): answer with numbers, caveats, glossary citations. Stretch: chart spec.

**Self-repair:** SQL execution error → back to node 3 once with the error attached (`repair_count ≤ 1`).
**Memory:** LangGraph checkpointer keyed by conversation ID (in-memory; "Redis in real prod" is the interview answer). Enables follow-ups ("now break that down by region").

### LLM provider pattern

Thin `LLMProvider` interface → `AnthropicProvider` (anthropic SDK, retries + backoff, token counting, max_tokens caps). Mirrors the resume's "Azure OpenAI + Anthropic API pattern" — the abstraction is the story. Default model `claude-sonnet-5`, configurable. Prompts are versioned files in `prompts/`; version logged with every request.

## RBAC & Admin Console (approved addition)

Two app roles, JWT-based:

- **Auth:** two fixed demo accounts — `analyst@demo`, `admin@demo` — bcrypt hashes in Secrets Manager, no signup. `/auth/login` issues a JWT carrying the role; a FastAPI dependency enforces it per endpoint; React routes by role. Prod story: swap credential store for Cognito/Entra SSO, role plumbing unchanged.
- **Analyst:** chat copilot; Snowflake session uses `COPILOT_APP_RO` → masked PII.
- **Admin:** everything analyst has, plus the **Admin Console**; Snowflake session uses `COPILOT_ADMIN` → unmasked. "Same query, two roles, masked vs unmasked" is one live demo moment combining app RBAC with warehouse governance.
- **Admin Console** (React route `/admin`, role-gated; served by `/admin/*` endpoints): overview tiles (request volume, error rate, latency p50/p95 incl. retrieval, token usage + est. cost), eval-accuracy trend (drift), feedback browser (filter 👎, read comments), per-request traces (question → intent → SQL → status, repair events flagged), link out to the CloudWatch dashboard for infra metrics.
- **Data source:** new `COPILOT.REQUEST_LOG` table — request_id, conversation_id, user_role, question, intent, sql, status, error_type, e2e_ms, retrieval_ms, tokens_in/out, prompt_version, ts. Admin console reads REQUEST_LOG + FEEDBACK + EVAL_RESULTS from Snowflake; infra metrics stay in CloudWatch. One store per concern.

## Snowflake design (approved)

Database `MEDTECH_ANALYTICS`, schemas BRONZE / SILVER / GOLD / COPILOT.

- **Bronze**: RAW_CENTERS, RAW_MACHINES, RAW_UTILIZATION, RAW_SERVICE_TICKETS — CSVs generated by a Python seed script with realistic mess (dupes, nulls, mixed date formats), loaded via COPY INTO.
- **Silver** (dbt models + tests: unique, not_null, accepted_values): CENTERS, MACHINES, MACHINE_UTILIZATION_DAILY, SERVICE_TICKETS — deduped, typed, standardized.
- **Gold** (dbt star schema, clustered on date/machine): DIM_TREATMENT_CENTER (~60 centers; contact_email under masking policy), DIM_MACHINE (~200 linacs; realistic model names — TrueBeam, Halcyon, Ethos), DIM_DATE, FACT_MACHINE_UTILIZATION (2 years daily grain, ~146k rows: planned/delivered fractions, uptime/downtime hrs, downtime_reason), FACT_SERVICE_TICKET (~8k rows: severity, category, resolution_hrs, parts_cost), V_CENTER_MONTHLY_KPIS.
- **COPILOT (AI Data Library)**: GLOSSARY (~40 terms + VECTOR(FLOAT,768) embeddings via Cortex EMBED_TEXT_768), SCHEMA_CARDS (per-table cards + embeddings), DATA_CONTRACTS (YAML in repo, loaded to table: owner, SLA, PII flags), FEEDBACK, EVAL_RESULTS, REQUEST_LOG.
- **Governance**: app role `COPILOT_APP_RO` = SELECT on GOLD + COPILOT only (separate writer role for FEEDBACK/EVAL_RESULTS/REQUEST_LOG); `COPILOT_ADMIN` role sees contact_email unmasked; masking policy + PII tag on contact_email (implemented as a CURRENT_ROLE()-based secure view over SILVER because the demo account is Standard edition; native masking policy + tag on Enterprise — see warehouse/governance.sql) (demo: same query, two roles, different results); vector search via `VECTOR_COSINE_SIMILARITY`. Fallback if trial region lacks Cortex: compute embeddings in Python into the same VECTOR columns — interface unchanged.

**Guardrails ×3 (interview talking point):** sqlglot validator in app → read-only enforcement in MCP server → Snowflake RO role.

## Production & ops layer (approved)

- **Eval harness** (`evals/`): ~30 golden cases in YAML (question → expected result assertions + expected tables), retrieval evals (expected glossary/schema hits), LLM-judge faithfulness scoring (rubric-based). Scorecard JSON; history written to COPILOT.EVAL_RESULTS.
- **CI/CD (GitHub Actions):** PR → ruff, pytest unit, eval smoke (5 cases live), docker build. Merge to main → image to ECR, ECS service update, React build to S3 + CloudFront invalidation, smoke test against live URL. Weekly scheduled full eval run → accuracy metric to CloudWatch (**this is the accuracy-drift monitoring claim**).
- **Telemetry:** CloudWatch EMF from FastAPI middleware — request volume, e2e latency, retrieval latency, error rate, token usage, SQL exec time. Structured JSON logs. Terraform-managed dashboard + error-rate alarm; dashboard opened live in interview.
- **Feedback loop:** 👍/👎 + comment → COPILOT.FEEDBACK with prompt version + conversation ID; analysis script clusters negative feedback by intent/table.
- **IaC (`infra/`, Terraform):** ECR, ECS Fargate service + ALB, S3 + CloudFront, CloudWatch dashboard/alarms, IAM, Secrets Manager (Anthropic key, Snowflake creds). Single small Fargate task; teardown script.
- **Testing:** unit (validator, schema parsing, prompt builders — recorded LLM fixtures, no API needed), integration (real Claude + Snowflake, marked, CI with secrets), React vitest smoke.
- **Docs:** README with architecture diagram, ARCHITECTURE.md with ADRs, RUNBOOK.md (backs the on-call story). Demo insurance: recorded 3-min video + screenshots.

## Interview prep deliverables (approved)

In `docs/prep/`, also published as private artifact pages:

1. **Question bank (~75 Q&A):** LLM/RAG fundamentals · agents/LangGraph/MCP · Snowflake & data engineering · AWS/production/MLOps · behavioral STAR stories mined from the resume · healthcare/med-device context (HIPAA-lite, why governance matters in med-device analytics) · drill-downs on this project itself · reverse questions per panelist.
2. **Panel strategy:** per-interviewer angles (hiring manager → business value/delivery/team fit; architects → schema design, governance, guardrails, scale), 45-min flow map, where to weave the project in.
3. **Demo script:** 5-min click path (analyst: question → SQL → answer → feedback; re-login as admin: masked→unmasked contrast + Admin Console → CloudWatch dashboard), rehearsed; fallback video + screenshots; architecture re-draw drill.

## Timeline (interview Thu Aug 13, 3:00 PM ET)

### Sat Aug 8 — tonight (~10 PM–midnight)
- **10:00–10:30** — Verify assets: Snowflake login works (note account locator + region for the Cortex check), AWS CLI configured, Anthropic key makes a test call.
- **10:30–11:30** — Repo scaffold (backend, frontend, mcp_server, infra, evals, dbt, docs, CI skeleton); seed-data generator written.
- **11:30–12:00** — Generate CSVs; create Snowflake database/schemas/warehouse; COPY INTO bronze. (Rolls to Sunday 9 AM if energy runs out.)

### Sun Aug 9 (9 AM–7 PM) — warehouse + vertical slice
- **9:00–10:30** — dbt silver models + tests (unique, not_null, accepted_values).
- **10:30–12:00** — Gold star schema + views + clustering; governance: roles, masking policy, PII tag; verify masked vs unmasked.
- **1:00–3:00** — Vertical-slice backend: `/chat` → AnthropicProvider → generate SQL → sqlglot validate → execute → summarize (linear pipeline, LangGraph comes Monday).
- **3:00–4:30** — Minimal React chat talking to it locally.
- **4:30–6:00** — Glossary + schema-card content; Cortex embeddings + vector search proven with a test query (fallback decision made here if Cortex unavailable).
- **6:00–7:00** — Buffer; commit. **EOD state: ask a question, get a real answer, locally.**

### Mon Aug 10 (9 AM–7 PM) — full agent + UI + auth
- **9:00–11:00** — MCP server (4 tools); executor rewired through MCP client.
- **11:00–1:00** — LangGraph graph: plan/retrieve/generate/validate/execute/summarize + repair loop + checkpointer memory; Pydantic-validated structured outputs with retry.
- **2:00–3:30** — Auth: `/auth/login` JWT + roles + bcrypt; role-per-Snowflake-session; React login + role routing.
- **3:30–5:30** — React polish: SQL panel, results table, feedback UI; REQUEST_LOG middleware.
- **5:30–7:00** — Unit tests (validator, schema parsing, prompt builders); local end-to-end rehearsal; commit.

### Tue Aug 11 (9 AM–7 PM) — AWS + CI/CD + evals + Admin Console
- **9:00–11:30** — Terraform: ECR, ECS Fargate + ALB, S3+CloudFront, Secrets Manager, IAM; first deploy.
- **11:30–12:30** — CloudWatch: EMF middleware, dashboard, error-rate + billing alarms; metrics verified flowing.
- **1:30–3:00** — GitHub Actions: PR pipeline + main deploy pipeline, both green.
- **3:00–4:30** — Eval harness: ~30 golden cases + retrieval evals + LLM judge; smoke subset wired into CI; weekly scheduled run publishing accuracy to CloudWatch.
- **4:30–6:00** — Admin Console: overview tiles, quality trend, traces, feedback browser.
- **6:00–7:00** — Full end-to-end test against the deployed URL; commit.

### Wed Aug 12 (9 AM–7 PM) — freeze at noon, then prep
- **9:00–12:00** — Bug-fix buffer; stretch items only if everything is green (charts in chat answers, Claude Desktop → MCP hookup); polish demo questions against real data.
- **12:00** — **BUILD FREEZE.**
- **1:00–3:00** — Finalize question bank (~75 Q&A), panel strategy, demo script (drafted in parallel during build days).
- **3:00–4:00** — Record 3-min fallback video + screenshots.
- **4:00–6:00** — Full rehearsal #1: demo click path twice, architecture re-draw on paper, out-loud answers to the top-20 questions.
- **6:00–7:00** — Mark weak answers for tomorrow; stop.

### Thu Aug 13 — interview day
- **9:30–10:30** — Light review: architecture re-draw from memory, top-10 answers out loud. No cramming after this.
- **10:30–11:00** — Logistics: Teams link + screen-share test, notifications off, app up (check dashboard), backup video within reach. *Confirm the calendar time — the invite header showed 12:00 PM (likely Pacific display); the body says 3:00 PM US/Eastern.*
- **2:30 PM** — Join buffer: water, notes card, browser tabs staged (app, GitHub repo, CloudWatch).
- **3:00–3:45 PM** — Interview.
- **After** — Send thank-you notes (drafted in advance, personalized per panelist).

## Risks & mitigations

- **Cortex functions unavailable in trial region** → Python-side embeddings into the same VECTOR columns.
- **Build overruns** → vertical-slice ordering means the app is always demoable; stretch items (charts, Claude Desktop hookup) cut first; Wednesday noon is hard freeze.
- **Demo-day failure** → recorded video + screenshots; local docker-compose run as second fallback.
- **AWS cost surprise** → single tiny Fargate task, teardown script, billing alarm in Terraform.
