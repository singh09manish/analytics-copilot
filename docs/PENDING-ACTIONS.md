# Pending actions

Phase 2 is merged, live-verified, and tagged `v0.2-agent`. No open Snowsight action
remains from that phase — see "Admin role read access to the ops tables" under Done
below, which used to be listed here as Open.

---

## Done

- **Admin role read access to the ops tables (ACCOUNTADMIN, Snowsight)** —
  applied and verified live, moved here from "Open" once confirmed rather than
  assumed:

  ```sql
  USE ROLE ACCOUNTADMIN;
  GRANT SELECT ON ALL TABLES IN SCHEMA MEDTECH_ANALYTICS.COPILOT TO ROLE COPILOT_ADMIN;
  GRANT SELECT ON FUTURE TABLES IN SCHEMA MEDTECH_ANALYTICS.COPILOT TO ROLE COPILOT_ADMIN;
  ```

  Verified against the live account as `COPILOT_ADMIN`: `REQUEST_LOG` (12 rows),
  `FEEDBACK` (3 rows), and `EVAL_RESULTS` (0 rows, readable — no eval run has
  written to it yet) are all readable. That confirms the first statement (`ALL
  TABLES`) is in effect.

  **Caveat, stated rather than glossed:** `ALL TABLES` grants only the tables
  that existed in the schema at the moment it ran; it says nothing about
  whether the second statement (`FUTURE TABLES`) also took effect, and reading
  three tables that already existed cannot distinguish the two. That only
  matters the next time a new table is added to the `COPILOT` schema — confirm
  it the same way the analyst-side FUTURE-tables *revoke* was already confirmed
  (see the re-grants bullet below): create a throwaway `COPILOT` table and
  check `COPILOT_ADMIN` can read it with no additional grant, then drop it.

  Why this was needed: `COPILOT_ADMIN` never had its own grant on
  `REQUEST_LOG`, `FEEDBACK`, or `EVAL_RESULTS` — it reached them by
  *inheriting* `COPILOT_APP_RO` (bootstrap.sql line 23). The Phase 2 security
  fix revoked the analyst's schema-wide SELECT on `COPILOT` (so an analyst
  could no longer read every user's questions and feedback), and that silently
  took admin's monitoring access with it. The lesson worth keeping: a
  monitoring role should not depend on what the least-privileged role happens
  to be allowed to read. Nothing in the copilot's user-facing path was ever
  affected — the SQL guard blocks the `COPILOT` schema outright, so the LLM
  cannot reach those tables under any role. This grant is for **app-authored**
  queries only: the Admin Console and the eval harness.
  `warehouse/bootstrap.sql` already contains both statements, so a fresh
  account gets them automatically going forward.
- **Snowflake account reactivated** — same account (identifier in `.env`, not
  repeated here — see `docs/DECISIONS.md`, "AWS deployment" section, for where the
  line between identifier and credential sits), data intact (146,000 utilization
  rows, 12 glossary terms, 6 schema cards, embeddings all present).
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
