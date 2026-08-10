# Analytics Copilot — working notes

A chat assistant that lets business users query a governed Snowflake warehouse in
natural language. React SPA → FastAPI → LangGraph agent → MCP tool server → Snowflake,
with JWT RBAC mapping `analyst` to a masked read-only session and `admin` to an
unmasked one.

## Read these first

- [docs/FLOW.md](docs/FLOW.md) — how the code actually executes, entrypoint onward.
- [docs/DECISIONS.md](docs/DECISIONS.md) — every meaningful choice and why.
- [docs/PENDING-ACTIONS.md](docs/PENDING-ACTIONS.md) — anything waiting on a human.

## Keep the docs current

Both documents are load-bearing, not decoration. Update them **in the same commit** as
the change they describe:

- **DECISIONS.md** — add an entry whenever you choose between real alternatives, adopt or
  reject a library, or change something because a review or a live run proved the previous
  approach wrong. Record what else was on the table and what the trade-off costs. If a bug
  forced the decision, describe the bug — the scar tissue is the useful part.
- **FLOW.md** — update whenever you add or remove a hop in a path it documents: a new
  endpoint, a new graph node or edge, a changed call chain, a new defense layer. Keep the
  `file:line` references accurate; a stale line number is worse than none.

If a change touches neither, say so in the commit body rather than leaving it ambiguous.

## Environment

- `uv` is at `~/.local/bin` and is **not** on the default PATH. So are `aws`, `terraform`,
  and `gh`. Use explicit paths or export the PATH first.
- Backend tests: `cd backend && ~/.local/bin/uv run pytest -q -m "not live"` (hermetic, no
  credentials). Live tests need `.env` and a working warehouse: `make test-live`.
- Frontend: `cd frontend && npm test -- --run`.
- Lint: `make lint` from the repo root.
- The API refuses to start unless `JWT_SECRET` is set to something other than the
  published default and at least 32 bytes — that token is the authorization boundary.

## Invariants — do not break these without a DECISIONS.md entry

- `answer_question(...)` never raises. Failures come back as a `ChatResponse` with a typed
  `error_type` (`validation` / `llm` / `snowflake` / `retrieval`).
- `log_request(...)` never raises, but always logs on failure.
- `ChatResponse` fields are additive only.
- The executor contract is `(sql) -> (columns, rows)`, shared by `SnowflakeClient.run_query`
  and `McpExecutor`.
- Role mapping is fail-closed and comes only from the verified JWT: `admin` →
  `COPILOT_ADMIN`, everything else → `COPILOT_APP_RO`. Never from a request body.
- The SQL guard allowlists the `GOLD` schema only. Widening it has caused a real privacy
  hole before — see DECISIONS.md §3.
- Secrets live in `.env` locally and Secrets Manager in AWS. `.env`, `secrets/`, `*.p8`,
  and Terraform state stay gitignored.
