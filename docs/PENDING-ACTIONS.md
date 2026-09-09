# Pending actions

All four build phases are complete and tagged. **The AWS stack was destroyed on 2026-09-09**
once the demo was no longer needed — see DECISIONS.md, "The stack was destroyed, not parked".
The code, the Terraform, and the Snowflake warehouse all survive; only the running AWS
resources are gone. What follows is what still wants a human.

---

## The stack is down — what that changed

`make aws-down` destroyed 29 resources cleanly (exit 0, verified empty afterward: no ECS
clusters, load balancers, CloudFront distributions, ECR repos, S3 buckets, secrets, log
groups, or OIDC providers remain in the project AWS account).

- **https://d9hwkll0kck56.cloudfront.net is permanently dead**, not merely down — the domain
  no longer resolves. A re-created stack gets a *new* CloudFront domain. That URL appears in
  the study pack and in the follow-up email draft that was never sent.
- **The account-level OIDC hazard was a non-issue.** `aws_iam_openid_connect_provider.github`
  is an account singleton, but `aws iam list-roles` confirmed nothing outside this project
  trusted it. No collateral damage.
- **~$2/day stopped.** Fargate, the ALB, and five public IPv4 addresses were the bulk.

### Both GitHub workflows now fail — decide what to do with them

Neither has been touched yet. Both authenticate to AWS through the OIDC provider and the
IAM role that no longer exist, so both fail at the assume-role step:

- **`.github/workflows/evals.yml` runs on a cron, Mondays 06:17 UTC.** It reads its Anthropic
  and Snowflake credentials from the deleted `analytics-copilot/runtime` secret — `gh secret
  list` is empty, so there is no fallback. This one fires by itself, every week, forever.
- **`.github/workflows/deploy.yml` runs on every push to `main`.**

`gh workflow disable deploy.yml evals.yml` stops both; `gh workflow enable` reverses it.

### Stale GitHub repo variables

`APP_URL` and `CLOUDFRONT_DISTRIBUTION_ID` still point at the destroyed distribution.
`WEB_BUCKET` and `AWS_DEPLOY_ROLE_ARN` happen to remain correct for a re-up, since the bucket
name and role ARN are derived from the account id and project name.

### Snowflake was deliberately left alone

It is not in Terraform and nothing was dropped. `COPILOT_WH` is `AUTO_SUSPEND = 60`, and with
the ECS task gone nothing queries it, so it sits idle at storage-only cost for a dataset of a
few hundred megabytes. Dropping `COPILOT` and the roles is a separate, deliberate act if the
project is ever truly retired.

---

## If you ever bring it back

```
make aws-up && make aws-secret
```

Three things that bite in that order:

- The Secrets Manager secret was destroyed with **no recovery window**, so `make aws-secret`
  is mandatory, not optional — the task cannot start without it.
- `CLOUDFRONT_DISTRIBUTION_ID` and `APP_URL` are **not** stable across a destroy/create cycle.
  Re-run those two `gh variable set` lines from the README or the next deploy fails at CDN
  invalidation and smoke-tests a dead URL.
- Re-enable the two workflows if they were disabled.

**Terraform state is local and gitignored**, so any re-up must also run from this machine.

---

## Genuinely unverified — say so plainly if asked

- **The weekly scheduled eval never fired a real run.** `.github/workflows/evals.yml` was
  exercised repeatedly by hand (`make evals`) and passes `actionlint`, but the Monday schedule
  never triggered while the stack was up. It cannot succeed now — its credentials came from
  the deleted secret.
- **The `FUTURE TABLES` grant to `COPILOT_ADMIN`** is applied but unproven — no new COPILOT
  table has been created since. The `ALL TABLES` grant is verified working: admin reads
  REQUEST_LOG, FEEDBACK, and EVAL_RESULTS.

---

## Optional, none blocking

These are notes on code that still exists, relevant only if the stack is ever re-created.

- Raise `origin_read_timeout` in `infra/cdn.tf` from 60 to 120. The quota already permits it.
- The planner's table inventory in `agent/prompts.py` is hand-maintained against
  `data/ai_library/schema_cards.yaml`. A committed test pins every column name, but generating
  the inventory from the cards would remove the defect class entirely — it has already caused
  two bugs: six wrong column names, then a missing `parts_cost` that made a whole question
  category unanswerable.
- `Admin.tsx` fails all three sections together if any one endpoint errors, rather than
  degrading section by section.

---

## Done — the state you can rely on

- **Was live and verified end to end on AWS**: the same question returned `***MASKED***`
  contact emails for the analyst and real addresses for the admin. The Admin Console returned
  403 to an analyst and 200 with real ops data to an admin. The infrastructure that served
  this is gone; the evidence and the code are not.
- **Eval harness**: **36/36, mean score 1.00**, safety gate green. The trend is recorded in
  `COPILOT.EVAL_RESULTS`: 27/36 → 35/36 → 36/36 across one calibration and two real fixes.
  Retrieval evals 9/9 at mean recall 1.00.
- **Snowflake governance**: analyst blocked from `SILVER`, `BRONZE`, and the COPILOT ops
  tables; secondary roles pinned empty; 60s statement timeout. Still standing.
- **Tests**: 242 hermetic backend, 24 frontend, 5 live. The live ones need Snowflake, which
  survives; they never needed AWS.
- **Tags**: `v0.1-slice`, `v0.2-agent`, `v0.3-aws`, `v0.4-evals`.
