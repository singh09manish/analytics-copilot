# Decisions

Every meaningful choice in this project, and why it was made. Where a decision was
forced by something we discovered the hard way, the discovery is recorded too — the
scar tissue is usually more informative than the rule.

**Maintenance:** add an entry whenever you choose between real alternatives, adopt or
reject a library, or change something because a review or a live run proved the
previous approach wrong. Keep the format: what was decided, why, what else was on the
table, and what the trade-off costs. Entries are grouped by area, not by date; new
areas go at the end. See also [FLOW.md](FLOW.md) for how the code actually executes.

---

## 1. Project shape and strategy

### Monorepo with four deployable pieces
`backend/` (FastAPI + agent), `frontend/` (React SPA), `mcp_server/` (MCP tool server),
`warehouse/` (dbt + loaders), plus `data/` and `scripts/`. One repo means one commit can
change the SQL guard and the test that proves it, and one CI run covers the whole
system. Four separate repos would be more "correct" for independent release cadences we
do not have.

### Vertical slice first, then depth
Phase 1 built the thinnest possible path that touches every layer — question → retrieval
→ LLM SQL → guard → Snowflake → summarised answer — before anything was made good.
Every later phase deepened that path rather than adding a new one. The alternative
(build the warehouse fully, then the agent, then the UI) leaves you with nothing
demonstrable until the very end, which is the wrong risk profile when there is a fixed
deadline.

### Phases end at tags, and each tag is demoable
`v0.1-slice`, `v0.2-agent`, `v0.3-aws`. A tag is only applied after live verification
against real infrastructure, not after the code merges. This distinction mattered:
Phase 2 was merged and green on 171 hermetic tests while still containing a bug that
made the primary demo question fail (see §5, planner intent).

### Synthetic data in the employer's domain
Linacs, treatment centres, machine utilisation, service tickets. Domain-shaped data
makes the demo legible to the panel without touching anything real or regulated. The
generator is seeded (`random.Random(42)`) so a rebuild reproduces byte-identical data —
which turned out to matter when the Snowflake account was locked and a full rebuild was
on the table.

---

## 2. Warehouse and data

### Snowflake medallion: BRONZE → SILVER → GOLD, plus COPILOT
Raw loads land in BRONZE untouched, SILVER cleans and types, GOLD is the star schema the
copilot queries, and COPILOT holds AI/ops artefacts (glossary, schema cards, request log,
feedback, eval results). The LLM only ever sees GOLD. Separating the layers is what makes
"the model cannot reach raw data" a structural guarantee rather than a prompt instruction.

### dbt for SILVER and GOLD
dbt gives us `ref()` lineage, schema tests, and idempotent rebuilds for free, and it is
the tool this job description names. Hand-written SQL scripts would have been faster to
start and worse to maintain, with no test story.

### Key-pair auth with a `TYPE = SERVICE` user
Password auth for service accounts collides with MFA policies and cannot be rotated
cleanly. `TYPE = SERVICE` sidesteps the MFA requirement by design rather than by
exception. The private key is never committed; it lives in `secrets/` locally (gitignored)
and in Secrets Manager in AWS.

### Column masking via secure views, not masking policies
**Forced by the environment.** The trial account is Snowflake *Standard* edition, which
rejects `CREATE MASKING POLICY` outright ("Unsupported feature 'MASKING POLICY'"). The
replacement is a secure view over SILVER that CASEs on `CURRENT_ROLE()`:
`COPILOT_APP_RO` sees `***MASKED***`, `COPILOT_ADMIN` sees the real value. `SECURE` is
load-bearing — a non-secure view leaks the underlying data through the query plan.

The honest framing for an interview: this is an emulation, and native masking policies
are the right answer on Enterprise edition because they are declarative, centrally
managed, and cannot be forgotten on a new view.

### The mask also covers NULL
Seven of sixty centres genuinely have no `contact_email`. The masked view returns
`***MASKED***` for those too, so an analyst cannot tell "hidden from you" from "not on
file". That is the fail-closed choice: revealing which records are incomplete is itself a
small information leak.

### `DEFAULT_SECONDARY_ROLES = ()` plus a client-side session pin
**Discovered during a review, not designed.** Snowflake's default of
`DEFAULT_SECONDARY_ROLES = ('ALL')` silently grants a session every role the user holds,
so connecting as `COPILOT_APP_RO` still carried `COPILOT_ADMIN`'s privileges and the
"read-only" role could read SILVER. Fixed in two places, deliberately: server-side
`ALTER USER COPILOT_SVC SET DEFAULT_SECONDARY_ROLES = ()`, and client-side
`USE SECONDARY ROLES NONE` pinned on every new connection, executed before the connection
is published so a failure closes the connection rather than yielding an over-privileged
one. Either alone is a single point of failure; the server-side setting can be changed by
anyone with the console, and the client-side pin only protects connections this code
opens.

### `STATEMENT_TIMEOUT_IN_SECONDS = 60` on the warehouse
The account default is 172,800 seconds — two days. An LLM can generate a genuinely
expensive query, and the repair loop will cheerfully launch a second one after the first
"fails". Without a timeout the user sees an error while the warehouse keeps burning
credits.

### `COPY GRANTS` on the AI-library reload
`load_ai_library.py` uses `CREATE OR REPLACE TABLE`, which drops grants. Once the COPILOT
grants became per-table (§3), a routine `make ai-library` would have silently revoked the
analyst's access to the retrieval corpus. `COPY GRANTS` preserves them. Note the syntax
trap: in the column-definition form it goes *after* the closing paren; only the CTAS form
takes it before.

---

## 3. Security and governance

This is the part of the project with the most decisions per line of code, because it is
the part the panel is most likely to probe.

### Three independent SQL defense layers
1. **sqlglot guard** (`backend/src/copilot/sql_guard.py`) — parses the LLM's SQL and
   enforces: exactly one statement, SELECT only, GOLD schema only, no side-effecting
   functions, and a `LIMIT` clamped to 1000.
2. **MCP server re-validation** (`mcp_server/server.py`) — runs the same guard again and
   applies its own denylist, on the assumption that layer 1 might be bypassed or wrong.
3. **Snowflake grants** — the role genuinely cannot read what it is not granted.

The point of three layers is that each one assumes the others may fail. That principle
was vindicated repeatedly: every layer has, at some point in this project's history, been
the only thing standing between a query and data it should not have reached.

### Parse, don't regex
The guard uses sqlglot's AST rather than pattern matching, because SQL is not a regular
language and every regex-based attempt was defeated. Actual bypasses found and closed:
`SELECT ... INTO` (emits `CREATE TABLE`), `TABLE(RESULT_SCAN(...))` and `LATERAL` as
relation sources, a bare UDTF parsing as `Table(this=Anonymous)`, `LIMIT NULL` and
`LIMIT 99999` defeating the cap, nested CTE names shadowing the allowlist, and quoted
lowercase `"gold"` evading a case-sensitive schema check. Also: `UNION` stores its CTEs
under the args key `with_`, not `with` — a detail no amount of reasoning would have
produced.

### The denylist runs on the *normalized* SQL, not the input
**A real bypass, found in review.** The MCP server checked `SYSTEM$`/`GET_DDL` against the
original string but executed the sqlglot-regenerated string. `GET_DDL/*x*/('TABLE','...')`
did not match the regex, and the parse/regenerate round trip moved the comment out from
between the name and the paren — so a callable `GET_DDL(...)` reached Snowflake. The fix
was structural: check the parsed tree of the SQL that will actually execute. That also
closes `"GET_DDL"(...)` and removes the false positives on string literals.

**The general lesson**, and the best one-line version of it: *validate the artifact you
execute, not the artifact you received.*

### The side-effect denylist lives in layer 1, not only layer 2
Originally only the MCP server blocked `SYSTEM$*` and `GET_DDL`. But the API falls back to
direct execution if the MCP subprocess fails to start, and `USE_MCP` is an operator-
flippable setting — in either state the denylist silently vanished, while the layer-3
backstop was explicitly unverified. Moving it into `sql_guard.validate` means it holds
regardless of execution path; the MCP server keeps its own copy as defense in depth.

### The SQL guard allowlists GOLD only, not GOLD + COPILOT
**The most interesting bug in the project**, because no per-change review could have found
it. In Phase 1 the guard allowed the COPILOT schema, which held only the glossary and
schema cards — harmless. In Phase 2 the ops tables (`REQUEST_LOG`, `FEEDBACK`,
`EVAL_RESULTS`) moved into that same schema, and `COPILOT_APP_RO` had a schema-wide
`SELECT` grant. The result: an analyst could ask "show me everything in
COPILOT.REQUEST_LOG" and read every other user's questions, generated SQL, and free-text
feedback — with all three defense layers agreeing it was fine.

Both changes were individually correct. The vulnerability lived in their interaction, and
only a whole-branch review looking at the system at once could see it. Retrieval and the
MCP metadata tools still read COPILOT, but they do so with app-authored SQL that never
passes through the guard, so narrowing the allowlist cost nothing.

### Admin gets its own grants, not inherited ones
Fixing the above broke admin monitoring, because `COPILOT_ADMIN` had never had a grant on
the ops tables — it reached them by inheriting `COPILOT_APP_RO`. Revoking the analyst's
schema-wide read took admin's access with it. The rule this produced: **a monitoring role
must not depend on what the least-privileged role is allowed to read.**

### The JWT is the authorization boundary, so the app refuses to start with a weak one
The role claim in the JWT chooses the Snowflake session, so forging one is equivalent to
privilege escalation. Startup rejects the published default secret, and — after a review
noted that `JWT_SECRET=x` sailed through the first version of the check — anything shorter
than 32 bytes.

### Role never comes from the client
The Snowflake role is derived solely from the verified JWT (`admin` → `COPILOT_ADMIN`,
everything else → `COPILOT_APP_RO`, fail-closed). The MCP server independently validates
the role against a strict allowlist, so even a lying client cannot open an arbitrary
session. Probes confirmed extra body fields, forged `role` claims, whitespace variants,
and SQL-injection-shaped role strings all fail closed.

### Conversation memory is namespaced by server-side identity
The LangGraph checkpointer is process-global and keyed by conversation id. Passing the
client's id straight through would let an authenticated user supply someone else's id and
have that conversation's history injected into their prompts. The key is
`f"{email}:{conversation_id}"` with the email taken from the verified token, and a
conversation id containing `:` is rejected with 400 so the separator cannot be forged.

### Known limitations, accepted deliberately
Recorded so they read as decisions rather than oversights: `/feedback` has no ownership
check or rate limit; there is no login rate limiting and `authenticate()` returns early
for unknown emails (a timing oracle that reveals nothing, since both demo accounts are
public); the JWT lives in `localStorage` and is therefore XSS-readable, where the
production answer is an httpOnly cookie plus a CSRF token.

---

## 4. LLM integration

### A thin `LLMProvider` protocol, with `AnthropicProvider` behind it
The interface is `structured(system, user, schema) -> LLMResult` and
`text(system, user) -> LLMResult`. The abstraction earns its keep in three ways: tests
inject a fake with zero network, the model is swappable by configuration, and it is the
honest implementation of the "Azure OpenAI + Anthropic" pattern rather than a claim about
one. LangChain's model wrappers would have supplied this, but with far more surface area
than two methods justify.

### Structured output via forced tool use plus Pydantic, with one retry
Every structured call forces a tool invocation whose schema is generated from a Pydantic
model, and the result is validated. On validation failure the error text is appended and
the call is retried exactly once; a second failure raises a typed error. Free-text JSON
parsing was rejected — it fails in ways that are tedious to detect and impossible to type.

### Truncation and refusal are not "no tool call"
An early version did `next(block for block in response if block.type == "tool_use")`,
which raises `StopIteration` when the model refuses or the response is truncated —
turning a routine API outcome into a crash. The provider now returns a three-tuple
carrying the response and error text, and branches on whether a `tool_use` block is
present (`provider.py:41-44`) rather than on `stop_reason` — `stop_reason` is only
interpolated into the error text for diagnostics, not checked — and routes the miss into
the retry path.

### `claude-sonnet-5`, configurable
Good enough for schema-constrained SQL generation at meaningfully lower cost and latency
than a larger model, and the whole point of the provider abstraction is that this is one
environment variable.

### Prompts are versioned and the version is logged
`PROMPT_VERSION` is written to every `REQUEST_LOG` row. When prompt behaviour changes,
the log can tell the two populations apart — which is the difference between "accuracy
dropped" and "accuracy dropped after v3 shipped on Tuesday."

---

## 5. Agent design

### LangGraph `StateGraph`, not a hand-rolled loop or a LangChain agent
The flow has real branching (four intents), a bounded repair cycle, and per-conversation
memory. A `StateGraph` makes those explicit as nodes and edges you can point at, which is
worth a great deal when explaining the system to an architect. A ReAct-style agent looping
over tools would be less predictable and much harder to bound; a hand-rolled state machine
would be all of the work and none of the checkpointer.

### Nodes: plan → retrieve → generate → validate → execute → summarize → remember
Each does one thing and writes disjoint state keys, which is what makes the conditional
edges readable and the failures attributable to a specific stage.

### Intent routing happens before retrieval
Smalltalk and unsupported requests must never generate SQL, and routing first means they
never pay for a retrieval round trip either.

### The repair loop is bounded at exactly one retry
On an execution error the graph returns to `generate` once with the error attached, then
fails cleanly. Unbounded repair is how you turn one bad query into a runaway spend loop;
one retry captures most genuinely recoverable errors (a wrong column name, a bad cast)
without that risk.

### A guard rejection does *not* trigger repair
Repair is for execution failures. If the deterministic guard rejected the SQL, the model
produced something structurally disallowed, and asking it to try again mostly invites a
second attempt at the same forbidden thing.

### The planner carries the table inventory in its own prompt
**Found only by live verification, after 171 hermetic tests passed.** The plan node runs
before retrieval, so it had no schema context at all — it classified against a one-line
domain description. Any question naming a specific *column* fell outside that mental
model: "show me treatment centres and their contact emails" returned `unsupported` five
times out of five, and that is precisely the question the analyst-versus-admin masking
demo is built on. It also appears to have judged contact emails as private and declined on
its own initiative.

Two changes: the planner now carries the table and column inventory, and it is told
explicitly that sensitivity is decided downstream by masking and role grants, not by the
classifier. Measured after: 24/24 correct across all four intents.

**The lesson worth carrying:** the fakes returned whatever intent the test wanted, so the
hermetic suite proved the *routing* worked while the real classifier was refusing the
question. That is the case for a small live suite even when unit tests are green — and
exactly what an eval harness with golden questions catches on day one.

### `answer_question` never raises
The API contract is a `ChatResponse` with a typed `error_type`, never an exception. Every
node catches, and errors are classified by origin: `validation`, `llm`, `snowflake`,
`retrieval`. Three separate review rounds found holes in this — the graph construction
sitting outside the try block, provider exceptions that are not `ValueError` being
reported as warehouse failures, and a stale `exec_error` overriding a node's correct
classification on the repair cycle. Each is now covered by a regression test.

### A failed summarize keeps the data
If the final LLM call fails, the response still carries the rows and the SQL with
`error_type="llm"`. The user gets their answer table; only the prose is missing.

### The checkpointer is bounded, and in-process
`InMemorySaver` retains every checkpoint — roughly nine per turn, each a full copy of the
channel values including result rows. Measured at ~90 KB retained per turn with nothing
evicting it, which is unbounded growth on a public endpoint, and it means an admin's
unmasked rows sit in process memory indefinitely. It is now wrapped with LRU caps on both
conversations and checkpoints per conversation. Redis or DynamoDB is the multi-instance
answer and the interview answer; in-process is correct for a single-task demo.

---

## 6. MCP

### A real MCP server, not a metaphor
Four tools — `run_query`, `list_tables`, `describe_table`, `search_glossary` — over the
official Python SDK. It is genuinely useful beyond the app: Claude Desktop can attach to
the same server, which makes the point better than any diagram.

### stdio transport, and the server runs inside the backend container
The spec sketched a sidecar container over localhost HTTP. stdio is what the client is
tested against, and adding an HTTP transport days before a demo is new untested code on
the critical path. Shipping the server inside the image is still a real MCP server over a
real transport; the sidecar is the answer to "how would you scale this?" — independent
scaling, language independence, and a network boundary you can authenticate.

### A synchronous wrapper over the async client
The MCP SDK is async; FastAPI's sync endpoints run in a threadpool. `McpExecutor` owns a
background thread with its own event loop and submits work via
`run_coroutine_threadsafe`. Making the whole call path async would have been a larger
change to already-reviewed code for no user-visible benefit.

### A dead subprocess must fail fast, not hang
**A genuine availability cliff, found in review.** If the subprocess died, `_main`
returned and left the loop stopped but not closed, so `close()` was never called and the
executor looked healthy forever. Every request then burned the full 60-second timeout,
plus another 60 for the repair retry — permanently. Because `/healthz` is a sync endpoint
sharing the same threadpool, roughly 40 concurrent users would take the entire API down,
liveness probe included. There are two distinct death shapes (the loop can stay alive when
the reader ends cleanly on EOF), and both are now detected; the executor marks itself
broken, fails in milliseconds, and the API rebuilds it on the next request.

---

## 7. API and auth

### JWT with two fixed demo accounts
No signup, no user table: `analyst@demo` and `admin@demo`, bcrypt hashes supplied by
environment. The role plumbing — token → role → Snowflake session — is the part that
matters and is identical to what a real deployment does. Swapping the credential store for
Cognito or Entra changes only where the hash comes from.

### bcrypt for password hashing
Deliberately slow, salted per password, and boring. Not a place to be clever.

### One decode per request
The auth dependency decodes the token once and returns the payload. An earlier version
decoded twice and indexed `payload["sub"]` unguarded, so a validly-signed token missing a
claim produced a 500 where it should produce a 401 — and a token expiring between the two
decodes did the same.

### Dependencies are built once, under a lock, and published last
`_deps()` originally published `app.state.provider` before the Snowflake clients and the
MCP executor existed. Sync endpoints run in a threadpool, so concurrent cold-start
requests read half-initialised state — measured at seven of eight returning 500. Now
everything is built into locals and published in one step, under a lock, and an MCP
failure logs and degrades to direct execution instead of silently disabling itself
forever.

### `log_request` never raises, but it does log
Telemetry must not be able to break a chat response, so the writer swallows everything —
but a bare `except: pass` meant a wrong column count or a missing grant produced no signal
anywhere while the README claimed every request was logged. It now warns with a traceback.

### The audit row is attributable
`REQUEST_LOG` stores the namespaced `email:conversation_id`, not the raw client value.
Two users sending `conversation_id: "1"` were correctly isolated in memory but conflated
in the audit log, with no user identifier stored anywhere. An audit log that cannot name
the actor is not much of an audit log.

---

## 8. Frontend

### React + TypeScript + Vite
The job description names React. Vite is the current default with no configuration to
argue about, and TypeScript catches the response-shape drift that a chat client is
otherwise prone to.

### No component library, no state manager
The whole UI is a login form, a message list, a SQL panel, a result table, and feedback
buttons. Component state and props cover it. Adding Redux or MUI would be more
configuration than feature.

### Auth in `localStorage`, with a client-side expiry check
`localStorage` survives a refresh, which matters during a demo. It is XSS-readable, and
that is stated as a known limitation rather than glossed. The client also decodes the
JWT's `exp` — without verification, purely as a UX check — so a stale token returns you to
the login screen at load instead of after a failed request. The 401 handler remains the
real backstop.

### Same-origin in production, proxy in development
The SPA calls `/api/*`. In AWS, CloudFront serves the SPA and forwards `/api/*` to the
ALB, so there is no cross-origin request and CORS stops existing as a problem. Vite's dev
server proxies `/api` to the local backend so development is unchanged.

### The generated SQL is always visible
Not a debug affordance — it is the product. A business user cannot audit an answer they
cannot see the derivation of, and every analyst in the room will want to read the SQL
before believing the number.

### Feedback buttons disable while in flight
A fast double-click, or 👍 then 👎 before the first resolved, sent two POSTs and wrote
duplicate rows, since `FEEDBACK` has no unique constraint on `request_id`.

---

## 9. Testing

### Hermetic by default, live behind a marker
`pytest -m "not live"` is the default and needs no credentials or network; live tests are
opt-in. Fast, deterministic tests are the ones that actually get run.

### Fakes, not mocks
`FakeProvider` and `FakeSnowflake` are small real classes that record what they were
asked. They read better than mock assertions and survive refactors that change call
shapes.

### Tests assert absence, not just presence
`test_smalltalk_never_generates_sql` asserts the SQL-generation call was *not* made — not
merely that `sql` came back null. The distinction catches a whole class of "right answer,
wrong path" bugs.

### Every review finding becomes a regression test
Each of the bypasses and race conditions in this document has a test named after the
behaviour it protects. Several were verified by reverting the fix and confirming the test
goes red — a test that has never failed has not been shown to test anything.

### A small live suite is not optional
Five live tests: the end-to-end slice, a multi-turn follow-up, an MCP round trip, the RBAC
masking difference, and rejection of an out-of-allowlist role. §5's planner bug is the
justification — 171 hermetic tests were green while the primary demo question was broken.

---

## 10. Tooling

### `uv` for Python
Fast, lockfile-based, and manages the interpreter as well as the dependencies. It lives at
`~/.local/bin` and is not on the default PATH, which has bitten this project more than
once — the Makefile now resolves it explicitly rather than assuming.

### `ruff` for linting
One fast tool covering what several slower ones used to. Its scope deliberately spans
`src tests ../data ../scripts ../warehouse ../mcp_server`, because the scripts and
warehouse code are the parts most likely to rot unnoticed.

### `sqlglot` for SQL parsing
Multi-dialect, understands Snowflake, and exposes an AST that can actually be walked.
The alternative — regex — is documented in §3 as the thing that kept failing.

### Documentation lives in the repo
The spec, the plans, the security review findings, and the fix records are all committed
under `docs/`. The review record in particular is worth keeping: it is the clearest
available evidence of how the engineering was actually done.

---

## 11. AWS deployment (Phase 3A — decided, being built)

### One CloudFront distribution with two origins
Default behaviour serves the SPA from a private S3 bucket via Origin Access Control;
`/api/*` forwards to the ALB. This is the decision that makes everything else work: it
provides HTTPS across the whole app without owning a domain, and it collapses the app to
a single origin so CORS is moot. The alternatives were an ACM certificate on the ALB
(requires a domain) or an HTTPS page calling an HTTP API (blocked as mixed content).

### No distribution-level SPA fallback for 403/404
`custom_error_response` is a *distribution*-level setting in CloudFront — it applies to
every behaviour, not just the default one. Adding the conventional SPA rule (403/404 →
200 + `/index.html`) would rewrite `/api/*` 403s and 404s into HTML too, so a mistyped or
undeployed API path would come back with `res.ok === true` and a `<!doctype html>` body:
`api.ts` would never throw `ApiError`, and `res.json()` would fail with an opaque
`SyntaxError` instead of a clean, visible 404. That converts exactly the failure this
kind of routing change risks — a missed or renamed route — from loud into silent. The
fallback is also solving a problem this app does not have: the SPA has no client-side
router (`main.tsx` renders `<App/>` directly), so `default_root_object = "index.html"`
already serves the one URL that exists, `/`. Left out. If client-side routing is ever
added, the fallback must be scoped to the default behaviour only — e.g. a CloudFront
Function on `viewer-request` for that behaviour — never the distribution-level setting,
or it will mask API errors again.

### GitHub Actions authenticates via OIDC
No long-lived AWS keys stored in the repo. The trust policy is scoped to branches in this
repo, so a fork's pull-request workflow cannot assume the role.

### CI builds the image; no local Docker
Docker is not installed on the developer machine and does not need to be — building in CI
is the production pattern anyway, and it means the image is proven to build on every push
rather than on one laptop.

### Fargate tasks run in public subnets with public IPs
The textbook layout is private subnets behind a NAT gateway, but a NAT gateway is ~$32 a
month, more than the rest of the stack combined, and the task only needs egress to
Snowflake and Anthropic. Inbound is restricted to the ALB's security group, so the public
IP is not an entry point. A deliberate cost trade, stated rather than hidden.

### Secrets in Secrets Manager, injected by the ECS agent
Terraform creates the secret container but never its contents — the values are pushed by a
separate script — so no secret value ever lands in Terraform state.

### Where "identifier" ends and "credential" begins, for this app's Snowflake values
**Found in the Task 8 review.** `scripts/aws_bootstrap_secret.py` treats `SNOWFLAKE_ACCOUNT`
as one of the six keys it pushes into Secrets Manager, alongside real credentials
(`ANTHROPIC_API_KEY`, `JWT_SECRET`, both password hashes, the Snowflake private-key PEM).
But `docs/PENDING-ACTIONS.md` had the live account identifier committed in plain text —
`git log -S` confirms it predates Phase 3A entirely, so nothing in this task introduced
it, but the two treatments disagreed with each other and that's worth resolving rather
than leaving as an inconsistency. The account identifier (`KETNSVS-VM01655`-shaped:
`<locator>.<region>`) is genuinely low-sensitivity — it names *which* Snowflake account to
connect to, the same way a hostname does, and getting in requires the private key, which
authenticates via key-pair auth and is never valid on its own. It has been redacted from
`docs/PENDING-ACTIONS.md` (referencing `.env` instead) so the two treatments agree, but
the redaction is about consistency, not a claim that the identifier was ever a meaningful
leak on its own. The line, going forward: the account identifier is routing information
(closer to a hostname or a repo name than a secret); the private key and the Anthropic API
key are credentials, full stop, and are the only two of the six bootstrap keys whose
disclosure alone grants access to something. The other three (`JWT_SECRET`, both bcrypt
hashes) sit in between — not identifiers, but not usable without also compromising this
app's own auth flow — and are treated as credentials because that's the safer default.

### Terraform state is local
An S3 and DynamoDB backend is the production answer and worth saying out loud, but
provisioning it is a second bootstrap problem for a single operator. State files are
gitignored, which means teardown must happen from the same machine.

### A teardown path exists from the first deploy
`make aws-down` runs `terraform destroy`. Buckets and registries are set to force-destroy
so teardown does not stall on leftover objects. The failure mode this avoids is an account
quietly accruing charges for resources nobody remembers creating.

### The deploy pipeline verifies the rollout is COMPLETED, not just stable
**Found in review.** `aws ecs wait services-stable` only polls until
`length(deployments) == 1 && runningCount == desiredCount`; it never inspects
`rolloutState`. A circuit-breaker rollback (below) ends at exactly that same state — one
deployment, back at steady count — because the rejected image never became the running
task. So a bad image reported the same waiter success as a good one, and the pipeline
would happily publish the SPA against a backend that never actually changed. The fix:
capture the PRIMARY deployment id from `update-service`, and after the wait, assert the
PRIMARY deployment is still that id and its `rolloutState` is `COMPLETED`, failing the job
otherwise. This also closes an eventual-consistency race where the waiter's first poll can
observe pre-update state and return success in seconds.

### The circuit breaker aborts bad deployments; it does not roll back content
`deployment_circuit_breaker { enable = true, rollback = true }` (`infra/ecs.tf`) stops the
waiter above from hanging for ~10 minutes on a crash-looping task, and it leaves the
already-running old task serving instead of going to zero. That is genuinely useful, but
"rollback" is not what it sounds like here. The deploy pipeline never registers a new task
definition revision — the deploy IAM policy deliberately grants no
`ecs:RegisterTaskDefinition` or `iam:PassRole` — so it deploys by forcing a new placement
of the *same* revision, whose container image reference is the mutable `:latest` ECR tag.
The pipeline moves `:latest` onto the new image before the rollout is even attempted, so
by the time the circuit breaker fires, `:latest` already points at the broken image
regardless of outcome. "Rollback" therefore means: the bad deployment attempt is aborted
and the previously-running task keeps running on the image it already pulled, but
`:latest` stays poisoned — any later replacement of that task (a host failure, an AZ
event, a manual restart) will pull `:latest` and silently adopt the broken image.

Genuine rollback would need either a human (or a follow-up pipeline step) to re-tag
`:latest` back onto a known-good `:<git-sha>` and force a new deployment, or a move to
per-SHA task definition revisions with real `RegisterTaskDefinition`/`PassRole` grants so
ECS's own revision-based rollback has something meaningful to revert to. Both are left out
of Phase 3A: the former is a manual runbook step, not yet automated; the latter widens the
deploy role's IAM surface for a demo-scale, single-operator system where a bad `:latest`
is caught by the pipeline's post-invalidation smoke-test step and fixed by hand within
minutes, not autonomously.

### `terraform validate` cannot catch API-level rejections — only a real apply can

**Found during the first live apply (Task 8).** `terraform fmt` and `terraform validate`
passed cleanly throughout Tasks 4–6, but the first real `terraform apply` still failed
partway on two errors neither one could have caught: `aws_security_group.alb`'s
description contained an apostrophe (`"...CloudFront's origin-facing ranges."`), which
`validate` accepts as a perfectly good HCL string but AWS's `CreateSecurityGroup` API
rejects at 400 — the security-group-description charset (`a-zA-Z0-9. _-:/()#,@[]+=&;{}!$*`)
has no apostrophe in it. And `aws_cloudfront_cache_policy.api` set `min_ttl`/`default_ttl`/
`max_ttl` to `0` (caching disabled) while also setting
`enable_accept_encoding_gzip`/`brotli` — valid per the provider's schema, but
`CreateCachePolicy` rejects those two parameters once a policy's TTLs mean there is no
cache key left for them to vary. Both are checked only by the destination API at apply
time, not by anything Terraform can verify offline: `validate` checks HCL syntax and
provider *schema* (types, required attributes, resource references), never the target
API's runtime value constraints. The fix for the cache policy was to stop hand-maintaining
one at all and reference AWS's managed `Managed-CachingDisabled` policy instead (id
`4135ea2d-6df8-44a3-9df3-4b5a84be39ad`, confirmed via `aws cloudfront list-cache-policies
--type managed` rather than trusted from memory) — it is purpose-built for exactly this
API origin case and cannot drift the way a custom policy can. This is exactly why the
plan sequenced a real `terraform apply` as its own task before the `v0.3-aws` tag, rather
than treating a clean `validate` as sufficient signoff on the infra code.

A third issue surfaced the same way, one step later: after both fixes above, `make
aws-secret` pushed the real secret values, and the documented cleanup step —
`terraform state rm aws_secretsmanager_secret_version.placeholder` — was run to stop
Terraform tracking the now-superseded placeholder version (see the removed comment this
replaces, previously in `infra/secrets.tf`). A follow-up `terraform plan`, run purely as a
verification step, showed `aws_secretsmanager_secret_version.placeholder will be created`
— `state rm` does not remove a resource's *declaration*, only Terraform's state pointer to
it, so with the block still present in `secrets.tf`, the very next `terraform apply` (by
anyone, for any reason — an unrelated infra change, or `make aws-up` re-run for routine
idempotency) would have recreated it with the hardcoded `"unset"` JSON and made it
`AWSCURRENT`, overwriting the live secret. That plan was never applied. The actual fix,
once `make aws-secret` has run at least once, is to delete the
`aws_secretsmanager_secret_version` resource from Terraform entirely rather than manage
its lifecycle at all — Terraform was never supposed to own this secret's contents (see
"Secrets in Secrets Manager, injected by the ECS agent" above), and the placeholder was
only ever there to give the ECS task definition something to reference before
`aws_bootstrap_secret.py` existed. `infra/secrets.tf` now has no
`aws_secretsmanager_secret_version` resource at all; a comment in its place documents why,
what populates the secret instead (`make aws-secret`, which must run before the first
deploy on a fresh account), and what happens if that step is skipped — the ECS task fails
to resolve its `secrets` block at container start and never comes up, a loud failure
rather than a silent or insecure one. `terraform plan` after the removal reported `No
changes. Your infrastructure matches the configuration.`, confirming there is no longer
any create/recreate hazard here.

This general pattern — `validate` and even a clean `plan` both agreeing a resource is fine
right up until state and configuration disagree about whether it still exists — is worth
remembering beyond this one secret: any resource with `lifecycle { ignore_changes }` used
to protect a value Terraform doesn't really own is a candidate for the same failure mode,
and the fix is usually to stop declaring the resource, not to keep patching around it.

---

## 12. Evals and telemetry (Phase 3B)

### EMF for the app's own metrics, not `PutMetricData`
The chat path (`backend/src/copilot/metrics.py`) emits CloudWatch Embedded Metric Format —
a `print()` of a JSON line with an `_aws` block — instead of calling `PutMetricData`
directly. The ECS task already ships stdout to CloudWatch Logs via the `awslogs` driver, so
EMF turns a log line CloudWatch would capture anyway into a metric with: no extra network
call on the request path, no `boto3` import in the hot path, and — the part that actually
shaped the task role — **no IAM permission at all**, because `PutMetricData` would need one
and reading a log stream the ECS agent already owns does not. `infra/iam.tf`'s comment on
`aws_iam_role.ecs_task` says it plainly: the task role is empty of AWS permissions by design,
and EMF is why adding metrics never had to be the thing that broke that.

The trade-off, paid deliberately: `emit()` (`metrics.py:17-34`) wraps the whole thing in a
bare `try/except: pass`. A malformed `_aws` block — a typo in a dimension name, a value
`json.dumps` can't serialize — is silently dropped: no exception, no metric, no signal
anywhere that it didn't land. That is why the shape is pinned by a test rather than trusted
to review, and why every dashboard widget in `infra/cloudwatch.tf` was written only after
reading the exact metric and dimension names at the `emit()` call sites (`api/main.py:
367-373`) rather than assumed — a widget naming a dimension that is never emitted renders
empty forever with no error either, the same failure mode one layer up.

The eval job (`copilot.eval.runner`'s `--publish`, `runner.py:192-219`) is the opposite
case, and deliberately so: it runs in GitHub Actions, not inside the ECS task, so it has no
log stream shipping to CloudWatch Logs to piggyback on. `PutMetricData` is the only way for
it to land `EvalAccuracy`/`EvalRetrievalRecall` in the same namespace, which is why
`infra/iam.tf` grants the GitHub deploy role a `cloudwatch:PutMetricData` permission the ECS
task role does not have and does not need. `PutMetricData` has no resource-level
permissions — `Resource` is always `"*"` for that action — so the `cloudwatch:namespace`
condition on that grant is the only thing keeping it scoped to `AnalyticsCopilot` instead of
every namespace in the account.

### Grading is mostly deterministic; the LLM judge is reserved for prose
`grade()` (`runner.py:42-67`) checks `error_type`, `intent`, and substrings in the generated
SQL or answer — string comparisons, no model call, no variance, and cheap enough to run on
every case every time. That covers most of the golden set, because most of it has a checkable
right answer: the SQL should mention `GOLD.DIM_MACHINE`, the guard should reject with
`validation`, the answer should contain `"98"`. An LLM judge (`eval/judge.py`) is invoked
only for cases whose correctness is genuinely a matter of prose quality — "does this glossary
answer actually explain MTTR" — where no substring check can distinguish a real answer from
one that merely contains the right keyword. Reserving the judge for that minority keeps the
harness fast, cheap, and reproducible for the majority of cases, and spends the
slower/costlier/noisier tool only where a cheaper one structurally cannot do the job.

### `safety-` case failures exit the run non-zero, independent of the pass rate
`run()` (`runner.py:139-162`) prints a scorecard for every case, but a failed `safety-` case
triggers `sys.exit(1)` regardless of how many other cases passed (`runner.py:156-161`). A
guard rejection is not a soft quality signal like "the SQL didn't mention the right table" —
it means one of the three SQL defense layers documented in §3 regressed, and a regressed
defense layer is exactly the kind of failure a green-looking scorecard (28/30, "97% mean
score") would otherwise bury. This mirrors the project's own history: §3's COPILOT-schema
exposure and §5's planner bug were both real regressions that a purely aggregate pass rate
would not have surfaced as urgent.

One golden case needed fixing to make this gate trustworthy rather than superstitious:
`safety-update-open-tickets` asked to "Update every open service ticket ... mark it as
resolved," which `plan_system()` (`agent/prompts.py`) correctly classifies as
`intent=unsupported` — a request to modify data, not read it. `unsupported` routes straight
to `scope_reply` and never reaches `generate()`/`validate()` at all, so the case failed on
an intent mismatch every time, never once exercising the guard. A `safety-` case that cannot
fail because the guard tripped is not testing the guard; it would have silently stopped
catching a real guard regression while still reporting the expected shape of failure. It is
now `safety-select-silver-staging`, a SELECT-shaped question that names a non-GOLD schema
directly (`MEDTECH_ANALYTICS.SILVER.SERVICE_TICKETS`) — a read the planner correctly
classifies as `data_query`, that reaches the guard, and that the guard rejects for a real,
verifiable reason (`schema SILVER is not allowed`).

**A known limitation, closed by removing the trigger rather than working around it (Final
review, Critical finding C3).** This workflow originally also carried a `pull_request:`
trigger running `--subset 5` as a cheap per-PR smoke check. It could not work: a PR branch
cannot assume the OIDC deploy role (the trust condition in `infra/iam.tf` only matches
`ref:refs/heads/main`), and duplicating Snowflake credentials as a second GitHub secret just
for this path was rejected for the same reason it was rejected for the weekly run (see below).
But `retrieve()` (`retrieval.py:40-69`) unconditionally needs a live Snowflake connection for
schema-card lookup before a `data_query` case's SQL is even generated, let alone validated —
so every PR subset run failed all five subset cases, including the `safety-` cases the subset
exists to protect, with `error_type="snowflake"` rather than exercising the guard: the exact
false-negative shape the `safety-update-open-tickets` fix above was written to eliminate,
reintroduced one layer up. Worse, only forks are excluded from repo secrets — a same-repo PR
still received `ANTHROPIC_API_KEY` — so this ran, and spent real Anthropic API calls to
manufacture five guaranteed FAILs, on every ordinary PR. Compounding both: GitHub's default
Linux shell has no `pipefail`, so `runner | tee scorecard.txt` silently discarded those FAILs'
exit code and the job still reported green (Final review, Critical finding C1) — three
independent defects stacked on one broken trigger.

The fix actually shipped is the simplest of the options considered: delete the trigger rather
than work around it. The guard already has real, hermetic, zero-cost coverage on every PR —
`backend/tests/test_sql_guard.py`, run by `ci.yml`, needs no warehouse and no API key — so a
live-but-broken PR subset was adding false confidence on top of that, not real coverage. Two
other options were considered and rejected as unnecessary for closing this gap, though either
remains available if a live-pipeline PR check is ever wanted: a scoped-down, read-only
Snowflake credential provisioned specifically for CI (a real new credential, not a duplicate
of the app's), or an offline/static schema-card fallback in `retrieve()` that would let
guard-only cases run with no warehouse at all.

### Admin endpoints return 403, not 404
`_require_admin` (`api/main.py:291-302`) raises 403 for an authenticated non-admin, not 404.
`_require_identity` already runs first and raises 401 for anyone unauthenticated, so by the
time the role check executes, the caller is known to be a real, authenticated user — the
route unquestionably exists and the only open question is whether this caller may use it.
Returning 404 there would be security-theater obscurity that costs real debuggability (an
analyst hitting `/api/admin/overview` by a stale bookmark sees a nonsensical "not found" for
a page that plainly exists in the same app) without buying any actual protection: the
endpoint's existence is not secret — it is in this repository — and an attacker who already
holds a valid token learns nothing from a 403 that a 404 would have hidden. 403 is the
honest status for "authenticated, and not allowed."

---

## 13. Process

### Subagent-driven development with mandatory review
Each task was implemented by a fresh agent with only that task's brief, then reviewed by a
separate agent that received the diff and the requirements but not the implementer's
reasoning. Findings entered a bounded fix loop. Independent review found problems that
self-review structurally cannot: the reviewer does not know what the implementer meant, so
it reads what the code says.

### Whole-branch review before merge, on the strongest model
Per-task review cannot see cross-task interactions. The COPILOT schema exposure (§3) and
the dead-MCP availability cliff (§6) were both found only at that stage, in code where
every individual change had already been approved.

### Reviewers verify by execution, not by reading
The most valuable findings came with reproductions — a working `GET_DDL` bypass, seven of
eight concurrent requests returning 500, a measured 90 KB per turn retained. A finding
with a repro is a fact; a finding without one is a hypothesis.

### Decisions are recorded where the reasoning happens
This file exists because most of the "why" above lived only in review transcripts and
would have been lost. If you are about to make a non-obvious choice, that is the moment to
write the entry — not later, when the reason has decayed into "that's how it works".
