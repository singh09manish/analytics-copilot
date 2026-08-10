# Pending actions — carried out of Phase 2

Phase 2 (LangGraph agent, MCP tool server, JWT RBAC) is code-complete and reviewed, but three
things could not be finished because the Snowflake trial account is locked and `.env` is
incomplete. This file is the checklist for closing them out.

---

## 1. Unlock Snowflake (blocks everything else)

The account is locked at the account level:

```
390507 (08001): Failed to connect to DB: KETNSVS-VM01655.snowflakecomputing.com:443.
Your account has been locked.
```

This is not a credential or code problem — the connection fails before authentication. On a trial
account it almost always means the trial window ended or the credit balance ran out.

**Option A (recommended): reactivate the existing account.** Sign into Snowsight and add a payment
method / convert to on-demand. Preserves the loaded data, the Cortex embeddings, all roles and
grants; `.env` stays as is; no rebuild.

**Option B: new trial on a different email.** Free, but a new account identifier means a new key
pair, a new `.env`, and a full rebuild — roughly 30–45 minutes, mostly unattended:

```bash
scripts/gen_keypair.sh                  # new key pair
# run warehouse/bootstrap.sql in Snowsight (see §2 — it now includes the tightened grants)
make seed load-bronze dbt-run ai-library
cd backend && uv run python ../scripts/verify_governance.py
```

The seed data is deterministic (`random.Random(42)`), so a rebuild reproduces byte-identical data.

## 2. Re-run these statements in Snowsight

`warehouse/bootstrap.sql` changed during the Phase 2 final review. On the **existing** account these
statements must be applied by hand (a fresh account gets them by running the whole file). Run them
as `ACCOUNTADMIN`, and **in this order** — the REVOKEs must precede the GRANTs.

```sql
ALTER WAREHOUSE COPILOT_WH SET STATEMENT_TIMEOUT_IN_SECONDS = 60;

REVOKE SELECT ON ALL TABLES IN SCHEMA MEDTECH_ANALYTICS.COPILOT FROM ROLE COPILOT_APP_RO;
REVOKE SELECT ON FUTURE TABLES IN SCHEMA MEDTECH_ANALYTICS.COPILOT FROM ROLE COPILOT_APP_RO;

GRANT SELECT ON TABLE MEDTECH_ANALYTICS.COPILOT.SCHEMA_CARDS TO ROLE COPILOT_APP_RO;
GRANT SELECT ON TABLE MEDTECH_ANALYTICS.COPILOT.GLOSSARY   TO ROLE COPILOT_APP_RO;
```

**Why:** the final review found that the analyst role could read `COPILOT.REQUEST_LOG` and
`COPILOT.FEEDBACK` — every other user's questions, generated SQL, and free-text feedback — and that
all three defense layers allowed it. Layer 1 was fixed in code (`ALLOWED_SCHEMAS` narrowed to
`{"GOLD"}`), but layer 3 should not depend on layer 1, hence the grant tightening.

**Verify the FUTURE-tables revoke actually took effect** — if it silently no-ops, any new COPILOT
table becomes analyst-readable again:

```sql
SHOW FUTURE GRANTS IN SCHEMA MEDTECH_ANALYTICS.COPILOT;
SHOW GRANTS TO ROLE COPILOT_APP_RO;
```

## 3. Fill in `.env` — the API will not start without it

The app now refuses to boot while `JWT_SECRET` is the published default, because the JWT is the
authorization boundary that picks the Snowflake role.

```bash
cd backend && uv run python ../scripts/gen_demo_users.py
```

It prompts for the analyst and admin demo passwords (these are what you type at the login screen in
the demo) and prints three lines — `DEMO_ANALYST_PASSWORD_HASH`, `DEMO_ADMIN_PASSWORD_HASH`, and
`JWT_SECRET` — to paste into `.env`.

## 4. Finish Task 10b: the live gauntlet and the tag

Once 1–3 are done:

```bash
export PATH="$HOME/.local/bin:$PATH"
make lint && make test          # hermetic: 169 backend + 18 frontend
make test-live                  # 5 live tests, written but never yet executed
```

The live tests (`backend/tests/live/test_slice_live.py`) cover the Phase 1 slice, a multi-turn
follow-up, the MCP executor round trip, the RBAC masking difference (analyst masked vs admin
unmasked through the MCP path), and rejection of an out-of-allowlist Snowflake role. They were
written against verified interfaces but have never run, so expect small adjustments — especially the
multi-turn test, which depends on live LLM SQL-generation shape.

Then the manual end-to-end: `make api` + `make web`, sign in as the analyst (ask a question, thumbs
it down with a comment), sign in as the admin in a second browser profile, and ask for treatment
centers with contact emails — the analyst should see `***MASKED***` where the admin sees real
addresses. Confirm `REQUEST_LOG` and `FEEDBACK` row counts increased.

Finally:

```bash
git tag v0.2-agent
```

---

## Known limitations carried into Phase 3 (deliberate, not oversights)

- `/feedback` has no ownership check or rate limit — a user can attach feedback to another user's
  `request_id`, and `FEEDBACK` has no submitter column.
- No login rate limiting; `authenticate()` returns early for unknown emails, a timing oracle that
  reveals nothing here because both demo accounts are public.
- The JWT lives in `localStorage`, so it is XSS-readable. The production answer is an httpOnly
  cookie plus a CSRF token.
- The checkpointer is bounded but in-process — Redis or DynamoDB is the multi-instance answer.
- `BoundedInMemorySaver` overrides only the sync `put`; async graph invocation would bypass the caps
  (nothing uses it today).
- CORS is hardcoded to `http://localhost:5173` and needs parameterizing before the AWS deploy.
