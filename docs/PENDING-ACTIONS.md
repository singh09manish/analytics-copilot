# Pending actions

All four build phases are complete and tagged. Nothing blocks the demo. What follows is the short
list that still wants a human, in the order it matters.

---

## Before the interview (Thu 2026-08-13, 3:00 PM ET)

### Warm the app up ~10 minutes before the call

The first request after an idle period pays a real cold-start cost: the ECS task opens Snowflake
connections and spawns the MCP subprocess lazily on first use. Ask one throwaway question through
the UI so the demo itself is fast.

Open https://d9hwkll0kck56.cloudfront.net, sign in, ask "how many machines do we have per model?",
wait for the answer, sign out. That is the whole warm-up. It also puts the first datapoints on the
CloudWatch dashboard, which is otherwise empty.

### Use two browser profiles for the RBAC demo

`frontend/src/api.ts` reads the token from `localStorage` on every request, so two tabs in the same
profile cannot hold two roles at once — signing in as admin silently replaces the analyst session,
and the side-by-side contrast would show real emails in both panes. Use a normal window for the
analyst and an Incognito window for the admin.

### Two live limits worth knowing

- **CloudFront caps origin responses at 60s.** A pathological query returns 504 at the edge while
  the backend keeps working. Normal questions answer well inside that. The quota (`L-AECE9FA7`,
  "Response timeout per origin") defaults to 120 and is adjustable, so it is raisable without a
  support case if you ever want the headroom.
- **`aws logs tail` does not exist on this machine.** The AWS CLI here is v1 at `~/.local/bin/aws`
  and `logs tail` is a v2 subcommand. Use
  `aws logs filter-log-events --log-group-name /ecs/analytics-copilot` instead.

---

## After the interview

### Tear the stack down

```
make aws-down
```

Roughly **$2/day** while it runs — Fargate, the ALB, and public IPv4 addresses across five ALB
subnets are the bulk. Three things to know before re-creating it later:

- It deletes `aws_iam_openid_connect_provider.github`, an **account-level singleton**. If this AWS
  account ever hosts another project using GitHub OIDC, that project loses its trust too.
- The Secrets Manager secret is destroyed with **no recovery window**, so `make aws-secret` must be
  re-run after any re-up.
- `CLOUDFRONT_DISTRIBUTION_ID` and `APP_URL` are **not** stable across a destroy/create cycle (the
  bucket name and role ARN are). Re-run those two `gh variable set` lines from the README, or the
  next deploy fails at CDN invalidation and smoke-tests a dead URL.

**Terraform state is local and gitignored**, so teardown must run from this machine.

---

## Genuinely unverified — say so plainly if asked

- **The weekly scheduled eval has never fired.** `.github/workflows/evals.yml` runs Mondays at
  06:17 UTC and on manual dispatch. The runner has been exercised repeatedly by hand (`make evals`)
  and the workflow passes `actionlint`, but the schedule has not yet triggered a real run. To see it
  end to end: `gh workflow run evals.yml`.
- **The `FUTURE TABLES` grant to `COPILOT_ADMIN`** is applied but unproven — no new COPILOT table has
  been created since. The `ALL TABLES` grant is verified working: admin reads REQUEST_LOG, FEEDBACK,
  and EVAL_RESULTS.

---

## Optional, none blocking

- Raise `origin_read_timeout` in `infra/cdn.tf` from 60 to 120. The quota already permits it.
- The planner's table inventory in `agent/prompts.py` is hand-maintained against
  `data/ai_library/schema_cards.yaml`. A committed test pins every column name, but generating the
  inventory from the cards would remove the defect class entirely — it has already caused two bugs:
  six wrong column names, then a missing `parts_cost` that made a whole question category
  unanswerable.
- `Admin.tsx` fails all three sections together if any one endpoint errors, rather than degrading
  section by section.

---

## Done — the state you can rely on

- **Live**: https://d9hwkll0kck56.cloudfront.net — React SPA, JWT auth, analyst and admin roles.
- **Verified end to end on AWS**: the same question returns `***MASKED***` contact emails for the
  analyst and real addresses for the admin. The Admin Console returns 403 to an analyst and 200 with
  real ops data to an admin.
- **Eval harness**: **36/36, mean score 1.00**, safety gate green. The trend is recorded in
  `COPILOT.EVAL_RESULTS`: 27/36 → 35/36 → 36/36 across one calibration and two real fixes.
  Retrieval evals 9/9 at mean recall 1.00.
- **Snowflake governance**: analyst blocked from `SILVER`, `BRONZE`, and the COPILOT ops tables;
  secondary roles pinned empty; 60s statement timeout.
- **Admin ops-table grant**: applied and verified live (moved here from "Open" once confirmed rather
  than assumed).
- **Tests**: 242 hermetic backend, 24 frontend, 5 live.
- **Tags**: `v0.1-slice`, `v0.2-agent`, `v0.3-aws`, `v0.4-evals`.
- **Dashboard**: [analytics-copilot-overview](https://us-east-1.console.aws.amazon.com/cloudwatch/home?region=us-east-1#dashboards:name=analytics-copilot-overview)
