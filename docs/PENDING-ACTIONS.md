# Pending actions

Phase 2 is merged, live-verified, and tagged `v0.2-agent`. One Snowsight action remains.

---

## Open: grant the admin role read access to the ops tables (ACCOUNTADMIN, Snowsight)

```sql
USE ROLE ACCOUNTADMIN;
GRANT SELECT ON ALL TABLES IN SCHEMA MEDTECH_ANALYTICS.COPILOT TO ROLE COPILOT_ADMIN;
GRANT SELECT ON FUTURE TABLES IN SCHEMA MEDTECH_ANALYTICS.COPILOT TO ROLE COPILOT_ADMIN;
```

**Why this is needed.** `COPILOT_ADMIN` never had its own grant on `REQUEST_LOG`, `FEEDBACK`, or
`EVAL_RESULTS` — it reached them by *inheriting* `COPILOT_APP_RO` (bootstrap.sql line 23). The
Phase 2 security fix revoked the analyst's schema-wide SELECT on `COPILOT` (so an analyst could no
longer read every user's questions and feedback), and that silently took admin's monitoring access
with it. The lesson worth keeping: a monitoring role should not depend on what the
least-privileged role happens to be allowed to read.

Nothing in the copilot's user-facing path is affected — the SQL guard blocks the `COPILOT` schema
outright now, so the LLM cannot reach those tables under any role. This grant is for
**app-authored** queries: the Phase 3 Admin Console and the eval harness.

`warehouse/bootstrap.sql` already contains both statements, so a fresh account gets them
automatically; they only need applying by hand to the existing account. Verify with:

```sql
SHOW GRANTS TO ROLE COPILOT_ADMIN;
```

Then confirm from the app side:

```bash
cd backend && uv run python -c "
from copilot.snowflake_client import SnowflakeClient
print(SnowflakeClient(role='COPILOT_ADMIN').run_query(
    'SELECT COUNT(*) FROM MEDTECH_ANALYTICS.COPILOT.REQUEST_LOG'))"
```

---

## Done

- **Snowflake account reactivated** — same account (`KETNSVS-VM01655`), data intact
  (146,000 utilization rows, 12 glossary terms, 6 schema cards, embeddings all present).
- **`.env` complete** — `JWT_SECRET` (58 chars, not the default) and both bcrypt password hashes set;
  the API's startup gate passes.
- **Snowsight re-grants applied and verified** — statement timeout is 60s; analyst is blocked from
  `REQUEST_LOG`/`FEEDBACK`/`EVAL_RESULTS` and from `SILVER`, still reads `SCHEMA_CARDS`/`GLOSSARY`
  and `GOLD`. The FUTURE-tables revoke was confirmed by creating a new `COPILOT` table and checking
  the analyst could not read it (then dropping it).
- **Live gauntlet green** — 5/5 live tests, plus 171 hermetic backend and 18 frontend tests.
- **RBAC demo verified end to end through the API** — same question, analyst sees `***MASKED***`,
  admin sees real addresses; multi-turn follow-up correctly carried context; telemetry writes
  confirmed working.

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
- The masking secure view returns `***MASKED***` for centers whose `contact_email` is genuinely
  NULL (7 of 60), so the analyst cannot distinguish "hidden" from "not on file". That is the
  fail-closed choice and it avoids leaking which records are incomplete.
