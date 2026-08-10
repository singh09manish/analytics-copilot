# Analytics Copilot — Phase 3A (Container, Terraform, CI/CD, Live Deploy) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Get the Phase 2 app running on AWS at a real HTTPS URL, deployed by a GitHub Actions pipeline from a private repo, for roughly $1/day and tearable down with one command.

**Architecture:** One CloudFront distribution fronts everything: the default behaviour serves the React build from a private S3 bucket via Origin Access Control, and `/api/*` forwards to an internet-facing ALB in front of an ECS Fargate service running the FastAPI backend. That single choice buys HTTPS for the whole app without owning a domain, and makes the browser see one origin so CORS stops mattering. Fargate tasks run in the default VPC's public subnets with public IPs so they can reach Snowflake and Anthropic without paying for a NAT gateway. Every secret lives in Secrets Manager and is injected into the task as an environment variable; nothing sensitive is baked into the image or committed. GitHub Actions authenticates to AWS through OIDC — no long-lived access keys anywhere.

**Tech Stack:** Docker (built in CI, never locally), Terraform 1.9.8, AWS (ECR, ECS Fargate, ALB, S3, CloudFront, Secrets Manager, IAM, CloudWatch Logs), GitHub Actions, `aws` CLI v1, `gh` CLI 2.63.2.

## Global Constraints

- Repo name is `analytics-copilot`; the GitHub repo is **private**. UI title stays "Analytics Copilot".
- Run cost target **≈ $1–2/day**, and a teardown path must exist from the first deploy onward — never leave the account with resources nobody can find.
- Region is **us-east-1** for everything. CloudFront's ACM certificates only live there, and it is the cheapest region for this workload.
- **No local Docker.** Images are built and pushed by GitHub Actions. Any step that requires `docker build` on the developer machine is wrong.
- **No long-lived AWS keys in GitHub.** Actions authenticates via an IAM role assumed through GitHub's OIDC provider.
- Secrets (`ANTHROPIC_API_KEY`, `JWT_SECRET`, `DEMO_ANALYST_PASSWORD_HASH`, `DEMO_ADMIN_PASSWORD_HASH`, `SNOWFLAKE_ACCOUNT`, and the Snowflake private key PEM) come from Secrets Manager at runtime. `.env`, `secrets/`, and `*.p8` are already gitignored and must stay that way.
- Phase 1/2 interfaces stay LOCKED: `answer_question(...)` never raises, `ChatResponse` gains only additive fields, `SnowflakeClient.run_query(sql, params=()) -> tuple[list[str], list[tuple]]`, the executor contract `(sql) -> (columns, rows)`, role mapping analyst→`COPILOT_APP_RO` / admin→`COPILOT_ADMIN` fail-closed.
- The app must keep starting locally from `.env` exactly as it does today. Every config change is additive with a default that preserves current behaviour.
- All existing tests stay green: 171 hermetic backend, 18 frontend, 5 live. `make lint` covers `src tests ../data ../scripts ../warehouse ../mcp_server`.
- Commits use conventional prefixes. Phase 3A ends at tag `v0.3-aws`.

## Architecture Decisions (made here, with reasons — do not silently revisit)

**MCP runs as a subprocess inside the backend container, not as a sidecar.** The spec sketched a sidecar container reached over localhost HTTP. Our `McpExecutor` speaks stdio and is well tested that way; adding an HTTP transport is new, untested code on the critical path days before a demo. Shipping the stdio server inside the same image is honest — it is a real MCP server over a real transport — and the sidecar remains the answer to "how would you scale this?" (independent scaling, language independence, a network boundary you can authenticate). Record this in the README so the choice is visible rather than looking like an oversight.

**Terraform state is local.** An S3+DynamoDB backend is the production answer and is worth saying out loud in the interview, but provisioning it is a second bootstrap problem for a single-operator demo. `terraform.tfstate*` is gitignored. Say so in the README.

**Fargate tasks get public IPs in public subnets.** The textbook layout is private subnets behind a NAT gateway, but a NAT gateway is ~$32/month — more than everything else here combined — and the task only needs egress to Snowflake and Anthropic. The security group allows inbound only from the ALB's security group, so the public IP is not a way in. This is a deliberate cost trade, not ignorance; the README should say that too.

---

## File Structure

```
Dockerfile                       # NEW: backend image, built in CI only
.dockerignore                    # NEW: keep secrets/.env/node_modules out of build context
infra/
├── main.tf                      # NEW: providers, data sources (default VPC/subnets), locals
├── variables.tf                 # NEW: project name, region, image tag, github repo
├── ecr.tf                       # NEW: image registry + lifecycle policy
├── secrets.tf                   # NEW: Secrets Manager secret + version
├── iam.tf                       # NEW: ECS execution/task roles, GitHub OIDC provider + deploy role
├── ecs.tf                       # NEW: cluster, task definition, service, security groups, log group
├── alb.tf                       # NEW: ALB, target group, listener
├── cdn.tf                       # NEW: S3 bucket, OAC, CloudFront with S3 + ALB origins
├── outputs.tf                   # NEW: app_url, ecr_repository_url, bucket, distribution id
└── terraform.tfvars.example     # NEW: what the operator fills in
.github/workflows/
├── ci.yml                       # MODIFY: add terraform fmt/validate job
└── deploy.yml                   # NEW: build+push image, update ECS, publish SPA, invalidate CDN
backend/src/copilot/
├── config.py                    # MODIFY: +snowflake_private_key_pem, +cors_allow_origins, +aws_region
├── snowflake_client.py          # MODIFY: _load_private_key accepts PEM content, not just a path
└── api/main.py                  # MODIFY: CORS from settings, /healthz stays dependency-free
backend/tests/
├── test_config_container.py     # NEW: PEM-from-env and CORS parsing
scripts/
├── aws_bootstrap_secret.py      # NEW: push local .env values into Secrets Manager
└── aws_smoke.py                 # NEW: end-to-end check against the deployed URL
Makefile                         # MODIFY: aws-plan, aws-up, aws-down, aws-smoke targets
README.md                        # MODIFY: deployment section, architecture decisions, teardown
```

---

### Task 1: Container-ready configuration

The image has no `.env` and no key file on disk, so the two things that read from the filesystem need an environment-variable path. Both changes are additive and default to today's behaviour.

**Files:**
- Modify: `backend/src/copilot/config.py`
- Modify: `backend/src/copilot/snowflake_client.py`
- Modify: `backend/src/copilot/api/main.py`
- Test: `backend/tests/test_config_container.py`

**Interfaces:**
- Produces: `Settings` gains `snowflake_private_key_pem: str = ""`, `cors_allow_origins: str = "http://localhost:5173"`, `aws_region: str = "us-east-1"`. `Settings.cors_origin_list() -> list[str]`. `_load_private_key(path: str, pem: str = "") -> bytes`.

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_config_container.py`:

```python
"""The container has no .env and no key file on disk, so both have to work from
environment variables alone."""
import pytest

from copilot.config import Settings
from copilot.snowflake_client import _load_private_key


def _pem() -> str:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()


def test_load_private_key_prefers_pem_over_path():
    der = _load_private_key("does/not/exist.p8", pem=_pem())
    assert isinstance(der, bytes) and len(der) > 100


def test_load_private_key_falls_back_to_path(tmp_path):
    p = tmp_path / "k.p8"
    p.write_text(_pem())
    assert isinstance(_load_private_key(str(p)), bytes)


def test_load_private_key_reports_both_sources_when_missing():
    with pytest.raises(FileNotFoundError, match="SNOWFLAKE_PRIVATE_KEY_PEM"):
        _load_private_key("nope/missing.p8")


def test_cors_origin_list_splits_and_strips():
    s = Settings(cors_allow_origins="https://a.example , https://b.example")
    assert s.cors_origin_list() == ["https://a.example", "https://b.example"]


def test_cors_origin_list_default_is_local_dev():
    assert Settings().cors_origin_list() == ["http://localhost:5173"]
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd backend && ~/.local/bin/uv run pytest tests/test_config_container.py -v`
Expected: FAIL — `_load_private_key() got an unexpected keyword argument 'pem'` and `Settings` has no `cors_origin_list`.

- [ ] **Step 3: Extend Settings**

In `backend/src/copilot/config.py`, add these fields to `Settings` after `use_mcp`, and the method after them:

```python
    # Container deployments have no key file on disk; the PEM arrives from Secrets
    # Manager as an env var. Empty means "use snowflake_private_key_path" (local dev).
    snowflake_private_key_pem: str = ""
    # Comma-separated. Same-origin behind CloudFront makes this moot in AWS, but it
    # stays configurable so a split-origin deployment does not need a code change.
    cors_allow_origins: str = "http://localhost:5173"
    aws_region: str = "us-east-1"

    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_allow_origins.split(",") if o.strip()]
```

- [ ] **Step 4: Accept PEM content in the key loader**

In `backend/src/copilot/snowflake_client.py`, replace `_load_private_key` with:

```python
def _load_private_key(path: str, pem: str = "") -> bytes:
    if pem.strip():
        data = pem.encode()
    else:
        p = Path(path)
        if not p.is_absolute():
            p = REPO_ROOT / p
        if not p.exists():
            raise FileNotFoundError(
                f"No Snowflake private key: {p} does not exist and "
                "SNOWFLAKE_PRIVATE_KEY_PEM is empty. Set one of them.")
        data = p.read_bytes()
    key = serialization.load_pem_private_key(data, password=None)
    return key.private_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
```

Then find its call site in `SnowflakeClient._connection` and pass the PEM through. The existing line looks like `private_key=_load_private_key(s.snowflake_private_key_path),` — change it to:

```python
                private_key=_load_private_key(
                    s.snowflake_private_key_path, s.snowflake_private_key_pem),
```

- [ ] **Step 5: Drive CORS from settings**

In `backend/src/copilot/api/main.py`, replace the hardcoded middleware block:

```python
app.add_middleware(
    CORSMiddleware, allow_origins=["http://localhost:5173"],
    allow_methods=["*"], allow_headers=["*"],
)
```

with:

```python
def _cors_origins() -> list[str]:
    from copilot.config import get_settings

    return get_settings().cors_origin_list()


app.add_middleware(
    CORSMiddleware, allow_origins=_cors_origins(),
    allow_methods=["*"], allow_headers=["*"],
)
```

- [ ] **Step 6: Run the suites**

Run: `cd backend && ~/.local/bin/uv run pytest -q -m "not live"`
Expected: PASS, count is 171 + 5 = 176.

Run: `make lint` from the repo root. Expected: clean.

- [ ] **Step 7: Commit**

```bash
git add backend/src/copilot/config.py backend/src/copilot/snowflake_client.py \
        backend/src/copilot/api/main.py backend/tests/test_config_container.py
git commit -m "feat(config): accept Snowflake key PEM and CORS origins from the environment"
```

---

### Task 2: Backend container image

**Files:**
- Create: `Dockerfile`
- Create: `.dockerignore`
- Modify: `.github/workflows/ci.yml`

**Interfaces:**
- Produces: an image whose entrypoint serves the FastAPI app on `$PORT` (default 8000) and that contains `mcp_server/server.py` so the stdio MCP subprocess can spawn.

- [ ] **Step 1: Write `.dockerignore`**

Nothing sensitive may enter the build context, and the build must not be invalidated by local scratch:

```
.git
.github
.venv
**/.venv
**/__pycache__
**/node_modules
frontend/dist
.env
.env.*
secrets/
*.p8
*.pub
.superpowers/
docs/
data/seed/*.csv
**/.pytest_cache
**/.ruff_cache
terraform.tfstate*
infra/.terraform/
```

- [ ] **Step 2: Write the `Dockerfile`**

`config.py` computes `REPO_ROOT = Path(__file__).parents[3]`, so `backend/src/copilot/config.py` must sit three levels below the root the app treats as the repo. Copying `backend/` to `/app/backend/` preserves that. `mcp_server/` must be a sibling because `McpExecutor` spawns it by relative path.

```dockerfile
# syntax=docker/dockerfile:1
FROM python:3.13-slim AS base
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 UV_LINK_MODE=copy
COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /usr/local/bin/uv
WORKDIR /app/backend

# Dependencies first: this layer is cached until pyproject/uv.lock change.
COPY backend/pyproject.toml backend/uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

# Then the source. mcp_server/ is a sibling of backend/ because McpExecutor spawns
# ../mcp_server/server.py, and REPO_ROOT resolves to /app/backend.
COPY backend/src ./src
COPY backend/README.md* ./
COPY mcp_server /app/mcp_server
COPY data/ai_library /app/data/ai_library
RUN uv sync --frozen --no-dev

ENV PATH="/app/backend/.venv/bin:$PATH" PORT=8000
EXPOSE 8000

# Non-root: the task has no reason to run privileged.
RUN useradd --create-home --uid 10001 appuser && chown -R appuser /app
USER appuser

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,os; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",8000)}/healthz').read()"

CMD ["sh", "-c", "uvicorn copilot.api.main:app --host 0.0.0.0 --port ${PORT}"]
```

- [ ] **Step 3: Add a container build to CI**

The developer machine has no Docker, so CI is the only place the image is ever proven to build. Append this job to `.github/workflows/ci.yml`:

```yaml
  image:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: docker/setup-buildx-action@v3
      - name: Build image (no push)
        uses: docker/build-push-action@v6
        with:
          context: .
          push: false
          tags: analytics-copilot:ci
          cache-from: type=gha
          cache-to: type=gha,mode=max
```

- [ ] **Step 4: Add a Terraform validation job to CI**

Also append to `.github/workflows/ci.yml` (it will pass trivially until Task 4 adds files, and guards formatting from then on):

```yaml
  terraform:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: hashicorp/setup-terraform@v3
        with: {terraform_version: 1.9.8}
      - run: terraform -chdir=infra fmt -check -recursive
      - run: terraform -chdir=infra init -backend=false
      - run: terraform -chdir=infra validate
```

- [ ] **Step 5: Verify the ignore file actually excludes secrets**

Run from the repo root:

```bash
git check-ignore -v .env secrets/copilot_svc_key.p8 || echo "NOT IGNORED - stop and fix"
grep -c "secrets/" .dockerignore
```

Expected: both paths report as gitignored, and `.dockerignore` mentions `secrets/`.

- [ ] **Step 6: Commit**

```bash
git add Dockerfile .dockerignore .github/workflows/ci.yml
git commit -m "feat(docker): backend image with the MCP server alongside it"
```

---

### Task 3: Private GitHub repo and green CI

This is where the pipeline becomes real. It needs `gh` authenticated — the operator does that interactively; the implementer must not attempt to handle credentials.

**Files:**
- Modify: `.gitignore` (Terraform artefacts)

**Interfaces:**
- Produces: an `origin` remote pointing at the private repo, `main` pushed, CI green.

- [ ] **Step 1: Confirm the operator's prerequisites**

```bash
~/.local/bin/gh auth status
```

Expected: logged in. If this fails, STOP and report — `gh auth login` is interactive and belongs to the operator, not to you.

- [ ] **Step 2: Ignore Terraform artefacts before anything can be committed**

Append to `.gitignore`:

```
# Terraform
infra/.terraform/
infra/.terraform.lock.hcl
terraform.tfstate
terraform.tfstate.*
*.tfvars
!infra/terraform.tfvars.example
crash.log
```

- [ ] **Step 3: Create the private repo and push**

```bash
~/.local/bin/gh repo create analytics-copilot --private --source=. --remote=origin --push
```

Expected: the repo is created and `main` is pushed. Confirm with `~/.local/bin/gh repo view --json name,visibility`.

- [ ] **Step 4: Verify CI runs and is green**

```bash
~/.local/bin/gh run list --limit 3
~/.local/bin/gh run watch $(~/.local/bin/gh run list --limit 1 --json databaseId --jq '.[0].databaseId')
```

Expected: `backend`, `frontend`, `image`, and `terraform` jobs all succeed. If the `image` job fails, fix the Dockerfile — this is the first real proof it builds.

- [ ] **Step 5: Commit**

```bash
git add .gitignore
git commit -m "chore: ignore terraform state and vars"
git push
```

---

### Task 4: Terraform foundation — registry, secrets, identities

**Files:**
- Create: `infra/main.tf`, `infra/variables.tf`, `infra/ecr.tf`, `infra/secrets.tf`, `infra/iam.tf`, `infra/outputs.tf`, `infra/terraform.tfvars.example`

**Interfaces:**
- Produces: `aws_ecr_repository.app`, `aws_secretsmanager_secret.app`, `aws_iam_role.ecs_execution`, `aws_iam_role.ecs_task`, `aws_iam_role.github_deploy`, `aws_cloudwatch_log_group.app`. Later tasks reference these exact names.

- [ ] **Step 1: `infra/variables.tf`**

```hcl
variable "project" {
  description = "Name prefix for every resource."
  type        = string
  default     = "analytics-copilot"
}

variable "region" {
  type    = string
  default = "us-east-1"
}

variable "image_tag" {
  description = "ECR tag the ECS task runs. CI updates the service directly, so this only matters for the first apply."
  type        = string
  default     = "latest"
}

variable "github_repo" {
  description = "owner/name of the GitHub repo allowed to assume the deploy role via OIDC."
  type        = string
}

variable "app_model" {
  type    = string
  default = "claude-sonnet-5"
}
```

- [ ] **Step 2: `infra/main.tf`**

Using the default VPC avoids inventing a network — and avoids a NAT gateway, which would cost more than everything else here combined.

```hcl
terraform {
  required_version = ">= 1.9.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.60"
    }
  }
}

provider "aws" {
  region = var.region
  default_tags {
    tags = {
      Project   = var.project
      ManagedBy = "terraform"
    }
  }
}

data "aws_caller_identity" "current" {}

data "aws_vpc" "default" {
  default = true
}

data "aws_subnets" "default" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }
}

locals {
  name = var.project
}
```

- [ ] **Step 3: `infra/ecr.tf`**

```hcl
resource "aws_ecr_repository" "app" {
  name                 = local.name
  image_tag_mutability = "MUTABLE"
  force_delete         = true # a demo registry should not block terraform destroy

  image_scanning_configuration {
    scan_on_push = true
  }
}

# Untagged layers pile up on every CI push and are pure cost.
resource "aws_ecr_lifecycle_policy" "app" {
  repository = aws_ecr_repository.app.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep the 10 most recent images"
      selection    = { tagStatus = "any", countType = "imageCountMoreThan", countNumber = 10 }
      action       = { type = "expire" }
    }]
  })
}
```

- [ ] **Step 4: `infra/secrets.tf`**

Terraform creates the container but never the contents — the values are pushed by `scripts/aws_bootstrap_secret.py` in Task 7, so no secret ever passes through Terraform state.

```hcl
resource "aws_secretsmanager_secret" "app" {
  name                    = "${local.name}/runtime"
  description             = "Runtime secrets for the Analytics Copilot backend task."
  recovery_window_in_days = 0 # demo: allow immediate recreate after destroy
}

# Placeholder so the ECS task definition can reference specific JSON keys before the
# operator has pushed real values. aws_bootstrap_secret.py overwrites this wholesale.
resource "aws_secretsmanager_secret_version" "placeholder" {
  secret_id = aws_secretsmanager_secret.app.id
  secret_string = jsonencode({
    ANTHROPIC_API_KEY           = "unset"
    JWT_SECRET                  = "unset"
    DEMO_ANALYST_PASSWORD_HASH  = "unset"
    DEMO_ADMIN_PASSWORD_HASH    = "unset"
    SNOWFLAKE_ACCOUNT           = "unset"
    SNOWFLAKE_PRIVATE_KEY_PEM   = "unset"
  })

  lifecycle {
    ignore_changes = [secret_string] # the operator's real values must not be reverted
  }
}
```

- [ ] **Step 5: `infra/iam.tf`**

Three identities with different jobs: the execution role pulls the image and reads secrets, the task role is what the app itself is (deliberately near-empty), and the deploy role is what GitHub assumes.

```hcl
# --- ECS execution role: used by the ECS agent, not the app ---
resource "aws_iam_role" "ecs_execution" {
  name = "${local.name}-ecs-execution"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "ecs_execution_managed" {
  role       = aws_iam_role.ecs_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

# Injecting secrets as env vars is the execution role's job, so it -- not the task
# role -- needs the read. Scoped to this one secret.
resource "aws_iam_role_policy" "ecs_execution_secrets" {
  name = "read-runtime-secret"
  role = aws_iam_role.ecs_execution.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["secretsmanager:GetSecretValue"]
      Resource = [aws_secretsmanager_secret.app.arn]
    }]
  })
}

# --- Task role: the app's own identity. It talks to Snowflake and Anthropic over
# the internet with their own credentials, so it needs no AWS permissions at all.
# Phase 3B adds cloudwatch:PutMetricData here.
resource "aws_iam_role" "ecs_task" {
  name = "${local.name}-ecs-task"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

# --- GitHub OIDC: short-lived credentials instead of stored access keys ---
data "tls_certificate" "github" {
  url = "https://token.actions.githubusercontent.com/.well-known/openid-configuration"
}

resource "aws_iam_openid_connect_provider" "github" {
  url             = "https://token.actions.githubusercontent.com"
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = [data.tls_certificate.github.certificates[0].sha1_fingerprint]
}

resource "aws_iam_role" "github_deploy" {
  name = "${local.name}-github-deploy"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = aws_iam_openid_connect_provider.github.arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = { "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com" }
        # Only this repo, and only from a branch -- a fork's pull_request workflow
        # runs with a different sub and cannot assume this role.
        StringLike = {
          "token.actions.githubusercontent.com:sub" = "repo:${var.github_repo}:ref:refs/heads/*"
        }
      }
    }]
  })
}

resource "aws_iam_role_policy" "github_deploy" {
  name = "deploy"
  role = aws_iam_role.github_deploy.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["ecr:GetAuthorizationToken"]
        Resource = "*"
      },
      {
        Effect = "Allow"
        Action = [
          "ecr:BatchCheckLayerAvailability", "ecr:CompleteLayerUpload",
          "ecr:InitiateLayerUpload", "ecr:PutImage", "ecr:UploadLayerPart",
          "ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer",
        ]
        Resource = [aws_ecr_repository.app.arn]
      },
      {
        Effect   = "Allow"
        Action   = ["ecs:UpdateService", "ecs:DescribeServices"]
        Resource = "*"
      },
      {
        Effect   = "Allow"
        Action   = ["s3:PutObject", "s3:DeleteObject", "s3:ListBucket"]
        Resource = [aws_s3_bucket.web.arn, "${aws_s3_bucket.web.arn}/*"]
      },
      {
        Effect   = "Allow"
        Action   = ["cloudfront:CreateInvalidation"]
        Resource = [aws_cloudfront_distribution.web.arn]
      },
    ]
  })
}
```

- [ ] **Step 6: `infra/outputs.tf`**

```hcl
output "app_url" {
  description = "The single HTTPS entry point: SPA at /, API at /api/*."
  value       = "https://${aws_cloudfront_distribution.web.domain_name}"
}

output "ecr_repository_url" {
  value = aws_ecr_repository.app.repository_url
}

output "web_bucket" {
  value = aws_s3_bucket.web.bucket
}

output "cloudfront_distribution_id" {
  value = aws_cloudfront_distribution.web.id
}

output "github_deploy_role_arn" {
  description = "Set as the AWS_DEPLOY_ROLE_ARN repo variable in GitHub."
  value       = aws_iam_role.github_deploy.arn
}

output "secret_name" {
  value = aws_secretsmanager_secret.app.name
}

output "ecs_cluster" {
  value = aws_ecs_cluster.main.name
}

output "ecs_service" {
  value = aws_ecs_service.app.name
}
```

- [ ] **Step 7: `infra/terraform.tfvars.example`**

```hcl
# Copy to terraform.tfvars and fill in. terraform.tfvars is gitignored.
github_repo = "YOUR_GITHUB_USERNAME/analytics-copilot"
region      = "us-east-1"
```

- [ ] **Step 8: Format and validate**

Terraform cannot fully validate until Tasks 5 and 6 add the resources referenced above (`aws_s3_bucket.web`, `aws_cloudfront_distribution.web`, `aws_ecs_cluster.main`, `aws_ecs_service.app`), so at this point run only:

```bash
~/.local/bin/terraform -chdir=infra fmt -recursive
```

Expected: files reformatted in place, exit 0. Full `validate` runs at the end of Task 6.

- [ ] **Step 9: Commit**

```bash
git add infra/
git commit -m "feat(infra): ECR, Secrets Manager, and the three IAM identities"
```

---

### Task 5: Terraform compute — ALB and Fargate service

**Files:**
- Create: `infra/alb.tf`, `infra/ecs.tf`

**Interfaces:**
- Consumes: `aws_ecr_repository.app`, `aws_secretsmanager_secret.app`, `aws_iam_role.ecs_execution`, `aws_iam_role.ecs_task`, `data.aws_vpc.default`, `data.aws_subnets.default`, `local.name`, `var.image_tag`, `var.app_model`.
- Produces: `aws_ecs_cluster.main`, `aws_ecs_service.app`, `aws_lb.app`, `aws_lb_target_group.app`, `aws_cloudwatch_log_group.app`, `aws_security_group.alb`.

- [ ] **Step 1: `infra/alb.tf`**

```hcl
resource "aws_security_group" "alb" {
  name        = "${local.name}-alb"
  description = "Public ingress to the ALB."
  vpc_id      = data.aws_vpc.default.id

  ingress {
    description = "HTTP from anywhere (CloudFront fronts this with HTTPS)"
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_lb" "app" {
  name               = "${local.name}-alb"
  load_balancer_type = "application"
  security_groups    = [aws_security_group.alb.id]
  subnets            = data.aws_subnets.default.ids
}

resource "aws_lb_target_group" "app" {
  name        = "${local.name}-tg"
  port        = 8000
  protocol    = "HTTP"
  vpc_id      = data.aws_vpc.default.id
  target_type = "ip" # Fargate awsvpc tasks register by IP, not instance id

  health_check {
    path                = "/healthz"
    matcher             = "200"
    interval            = 30
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }

  # A deploy replaces targets; draining fast keeps rollouts short on a demo.
  deregistration_delay = 10
}

resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.app.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.app.arn
  }
}
```

- [ ] **Step 2: `infra/ecs.tf`**

```hcl
resource "aws_cloudwatch_log_group" "app" {
  name              = "/ecs/${local.name}"
  retention_in_days = 7 # demo retention; longer is pure cost here
}

resource "aws_ecs_cluster" "main" {
  name = local.name
}

resource "aws_security_group" "task" {
  name        = "${local.name}-task"
  description = "Backend task: inbound only from the ALB."
  vpc_id      = data.aws_vpc.default.id

  ingress {
    description     = "ALB to app port"
    from_port       = 8000
    to_port         = 8000
    protocol        = "tcp"
    security_groups = [aws_security_group.alb.id]
  }

  # Egress to Snowflake, the Anthropic API, and ECR.
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_ecs_task_definition" "app" {
  family                   = local.name
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 512
  memory                   = 1024
  execution_role_arn       = aws_iam_role.ecs_execution.arn
  task_role_arn            = aws_iam_role.ecs_task.arn

  container_definitions = jsonencode([{
    name      = "api"
    image     = "${aws_ecr_repository.app.repository_url}:${var.image_tag}"
    essential = true

    portMappings = [{ containerPort = 8000, protocol = "tcp" }]

    environment = [
      { name = "PORT", value = "8000" },
      { name = "APP_MODEL", value = var.app_model },
      { name = "USE_MCP", value = "true" },
      { name = "SNOWFLAKE_USER", value = "COPILOT_SVC" },
      { name = "SNOWFLAKE_WAREHOUSE", value = "COPILOT_WH" },
      { name = "SNOWFLAKE_DATABASE", value = "MEDTECH_ANALYTICS" },
      { name = "SNOWFLAKE_ROLE", value = "COPILOT_APP_RO" },
      # Same-origin behind CloudFront, so this only needs to cover local dev.
      { name = "CORS_ALLOW_ORIGINS", value = "http://localhost:5173" },
    ]

    # Injected by the ECS agent from Secrets Manager; never in the image or in state.
    secrets = [
      for k in [
        "ANTHROPIC_API_KEY", "JWT_SECRET", "DEMO_ANALYST_PASSWORD_HASH",
        "DEMO_ADMIN_PASSWORD_HASH", "SNOWFLAKE_ACCOUNT", "SNOWFLAKE_PRIVATE_KEY_PEM",
      ] : { name = k, valueFrom = "${aws_secretsmanager_secret.app.arn}:${k}::" }
    ]

    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.app.name
        "awslogs-region"        = var.region
        "awslogs-stream-prefix" = "api"
      }
    }
  }])
}

resource "aws_ecs_service" "app" {
  name            = local.name
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.app.arn
  desired_count   = 1
  launch_type     = "FARGATE"

  network_configuration {
    subnets         = data.aws_subnets.default.ids
    security_groups = [aws_security_group.task.id]
    # Public IP instead of a NAT gateway: the task needs egress to Snowflake and
    # Anthropic, and a NAT gateway costs more than the rest of this stack combined.
    # Inbound is still ALB-only via the security group.
    assign_public_ip = true
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.app.arn
    container_name   = "api"
    container_port   = 8000
  }

  # CI deploys by pushing a new image and forcing a new deployment, so Terraform
  # must not fight it by reverting the task definition on the next apply.
  lifecycle {
    ignore_changes = [task_definition, desired_count]
  }

  depends_on = [aws_lb_listener.http]
}
```

- [ ] **Step 3: Format**

```bash
~/.local/bin/terraform -chdir=infra fmt -recursive
```

- [ ] **Step 4: Commit**

```bash
git add infra/alb.tf infra/ecs.tf
git commit -m "feat(infra): ALB and Fargate service for the backend"
```

---

### Task 6: Terraform edge — S3, CloudFront, and the single-origin trick

This is the task that makes the whole thing work without owning a domain. One distribution, two origins: `/api/*` to the ALB, everything else to a private S3 bucket.

**Files:**
- Create: `infra/cdn.tf`

**Interfaces:**
- Consumes: `aws_lb.app`, `local.name`.
- Produces: `aws_s3_bucket.web`, `aws_cloudfront_distribution.web`.

- [ ] **Step 1: `infra/cdn.tf`**

```hcl
resource "aws_s3_bucket" "web" {
  bucket        = "${local.name}-web-${data.aws_caller_identity.current.account_id}"
  force_destroy = true # demo: let terraform destroy remove the built SPA
}

# The bucket stays private; only CloudFront may read it, via Origin Access Control.
resource "aws_s3_bucket_public_access_block" "web" {
  bucket                  = aws_s3_bucket.web.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_cloudfront_origin_access_control" "web" {
  name                              = "${local.name}-oac"
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

resource "aws_s3_bucket_policy" "web" {
  bucket = aws_s3_bucket.web.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "cloudfront.amazonaws.com" }
      Action    = "s3:GetObject"
      Resource  = "${aws_s3_bucket.web.arn}/*"
      Condition = {
        StringEquals = { "AWS:SourceArn" = aws_cloudfront_distribution.web.arn }
      }
    }]
  })
}

# API responses must never be cached, and the Authorization header has to survive
# the hop to the ALB -- CloudFront strips it by default.
resource "aws_cloudfront_cache_policy" "api" {
  name        = "${local.name}-api-nocache"
  min_ttl     = 0
  default_ttl = 0
  max_ttl     = 0

  parameters_in_cache_key_and_forwarded_to_origin {
    enable_accept_encoding_gzip = true
    cookies_config { cookie_behavior = "none" }
    headers_config { header_behavior = "none" }
    query_strings_config { query_string_behavior = "none" }
  }
}

resource "aws_cloudfront_origin_request_policy" "api" {
  name = "${local.name}-api-forward-all"

  cookies_config { cookie_behavior = "all" }
  query_strings_config { query_string_behavior = "all" }
  headers_config {
    header_behavior = "allViewerExceptHostHeader" # keeps Authorization, drops Host
  }
}

resource "aws_cloudfront_distribution" "web" {
  enabled             = true
  default_root_object = "index.html"
  comment             = local.name
  price_class         = "PriceClass_100" # NA + EU only; cheapest

  origin {
    origin_id                = "s3-web"
    domain_name              = aws_s3_bucket.web.bucket_regional_domain_name
    origin_access_control_id = aws_cloudfront_origin_access_control.web.id
  }

  origin {
    origin_id   = "alb-api"
    domain_name = aws_lb.app.dns_name

    custom_origin_config {
      http_port              = 80
      https_port             = 443
      # CloudFront terminates TLS for the browser and talks HTTP to the ALB inside
      # AWS. Putting a cert on the ALB would require owning a domain.
      origin_protocol_policy = "http-only"
      origin_ssl_protocols   = ["TLSv1.2"]
    }
  }

  default_cache_behavior {
    target_origin_id       = "s3-web"
    viewer_protocol_policy = "redirect-to-https"
    allowed_methods        = ["GET", "HEAD", "OPTIONS"]
    cached_methods         = ["GET", "HEAD"]
    compress               = true
    cache_policy_id        = "658327ea-f89d-4fab-a63d-7e88639e58f6" # AWS managed: CachingOptimized
  }

  ordered_cache_behavior {
    path_pattern             = "/api/*"
    target_origin_id         = "alb-api"
    viewer_protocol_policy   = "https-only"
    allowed_methods          = ["GET", "HEAD", "OPTIONS", "PUT", "POST", "PATCH", "DELETE"]
    cached_methods           = ["GET", "HEAD"]
    compress                 = true
    cache_policy_id          = aws_cloudfront_cache_policy.api.id
    origin_request_policy_id = aws_cloudfront_origin_request_policy.api.id
  }

  # A single-page app owns its own routing: unknown paths must return index.html,
  # not S3's 403.
  custom_error_response {
    error_code            = 403
    response_code         = 200
    response_page_path    = "/index.html"
    error_caching_min_ttl = 0
  }

  custom_error_response {
    error_code            = 404
    response_code         = 200
    response_page_path    = "/index.html"
    error_caching_min_ttl = 0
  }

  restrictions {
    geo_restriction { restriction_type = "none" }
  }

  viewer_certificate {
    cloudfront_default_certificate = true # *.cloudfront.net, no domain needed
  }
}
```

- [ ] **Step 2: Route the API under `/api` in the backend**

CloudFront forwards `/api/*` to the ALB with the path intact, so the backend must answer on those paths. Add a root-path prefix in `backend/src/copilot/api/main.py` immediately after the `app = FastAPI(...)` line:

```python
# CloudFront routes /api/* to the ALB with the path unchanged, so every route is
# mounted under /api. /healthz stays at the root for the ALB's own health check,
# which talks to the task directly and never goes through CloudFront.
```

Then change every route decorator except `/healthz` to carry the prefix: `@app.post("/api/auth/login")`, `@app.post("/api/chat")`, `@app.post("/api/feedback")`. Leave `@app.get("/healthz")` exactly as it is.

- [ ] **Step 3: Point the frontend at the same origin**

In `frontend/src/api.ts`, the base URL is currently a literal pointing at `http://localhost:8000`. Replace it with:

```typescript
// Same-origin in production: CloudFront serves the SPA and proxies /api/* to the
// ALB, so there is no cross-origin request and no CORS preflight. Vite's dev server
// proxies /api to the local backend (see vite.config.ts).
const BASE = "/api";
```

- [ ] **Step 4: Proxy `/api` in the Vite dev server**

So local development keeps working unchanged, add a `server.proxy` entry to `frontend/vite.config.ts`:

```typescript
  server: {
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
      },
    },
  },
```

- [ ] **Step 5: Update the tests that assert URLs**

The frontend tests assert fetch was called with specific paths. Run them, and update the asserted paths from `http://localhost:8000/...` to `/api/...`:

Run: `cd frontend && npm test -- --run`
Expected: failures naming the old URLs; fix each assertion, then re-run to green (18 passing).

Backend tests call the API through `TestClient`, so update those paths too:

Run: `cd backend && ~/.local/bin/uv run pytest -q -m "not live"`
Expected: failures in `tests/test_api.py` for 404s; change the client calls to `/api/...` and re-run to green (176 passing).

- [ ] **Step 6: Validate the whole Terraform config**

Now that every referenced resource exists:

```bash
~/.local/bin/terraform -chdir=infra fmt -recursive
~/.local/bin/terraform -chdir=infra init -backend=false
~/.local/bin/terraform -chdir=infra validate
```

Expected: `Success! The configuration is valid.`

- [ ] **Step 7: Commit**

```bash
git add infra/cdn.tf backend/src/copilot/api/main.py backend/tests frontend/src frontend/vite.config.ts
git commit -m "feat(infra): CloudFront fronting both the SPA and the API under one origin"
```

---

### Task 7: Deploy pipeline

**Files:**
- Create: `.github/workflows/deploy.yml`

**Interfaces:**
- Consumes: the Terraform outputs `github_deploy_role_arn`, `ecr_repository_url`, `web_bucket`, `cloudfront_distribution_id`, `ecs_cluster`, `ecs_service`.
- Produces: a `main`-push pipeline that builds, pushes, and rolls out.

- [ ] **Step 1: Write `.github/workflows/deploy.yml`**

```yaml
name: deploy
on:
  push: {branches: [main]}
  workflow_dispatch:

concurrency:
  group: deploy
  cancel-in-progress: false

permissions:
  id-token: write # required to request the OIDC token
  contents: read

env:
  AWS_REGION: us-east-1

jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - uses: aws-actions/configure-aws-credentials@v4
        with:
          role-to-assume: ${{ vars.AWS_DEPLOY_ROLE_ARN }}
          aws-region: ${{ env.AWS_REGION }}

      - uses: aws-actions/amazon-ecr-login@v2
        id: ecr

      - name: Build and push image
        uses: docker/build-push-action@v6
        with:
          context: .
          push: true
          tags: |
            ${{ steps.ecr.outputs.registry }}/analytics-copilot:${{ github.sha }}
            ${{ steps.ecr.outputs.registry }}/analytics-copilot:latest
          cache-from: type=gha
          cache-to: type=gha,mode=max

      - name: Roll out the new image
        run: |
          aws ecs update-service \
            --cluster analytics-copilot \
            --service analytics-copilot \
            --force-new-deployment >/dev/null
          aws ecs wait services-stable \
            --cluster analytics-copilot --services analytics-copilot

      - uses: actions/setup-node@v4
        with: {node-version: 22}

      - name: Build the SPA
        run: cd frontend && npm ci && npm run build

      # Hashed assets are immutable and cached hard; index.html must never be, or
      # browsers keep loading the previous build's asset names.
      - name: Publish the SPA
        run: |
          aws s3 sync frontend/dist "s3://${{ vars.WEB_BUCKET }}" \
            --delete --cache-control "public,max-age=31536000,immutable" \
            --exclude index.html
          aws s3 cp frontend/dist/index.html "s3://${{ vars.WEB_BUCKET }}/index.html" \
            --cache-control "no-cache,no-store,must-revalidate"

      - name: Invalidate the CDN
        run: |
          aws cloudfront create-invalidation \
            --distribution-id "${{ vars.CLOUDFRONT_DISTRIBUTION_ID }}" \
            --paths "/*" >/dev/null
```

- [ ] **Step 2: Verify the workflow parses**

```bash
python3 -c "import yaml,sys; yaml.safe_load(open('.github/workflows/deploy.yml')); print('deploy.yml parses')"
python3 -c "import yaml,sys; yaml.safe_load(open('.github/workflows/ci.yml')); print('ci.yml parses')"
```

Expected: both print their confirmation.

- [ ] **Step 3: Commit**

```bash
git add .github/workflows/deploy.yml
git commit -m "feat(ci): OIDC deploy pipeline for image, service, SPA, and CDN"
```

---

### Task 8: Secret bootstrap, first apply, and teardown

The only task that spends money. It needs AWS credentials configured — the operator does that; if `aws sts get-caller-identity` fails, STOP and report rather than trying to obtain credentials.

**Files:**
- Create: `scripts/aws_bootstrap_secret.py`, `scripts/aws_smoke.py`
- Modify: `Makefile`, `README.md`

**Interfaces:**
- Consumes: everything above.
- Produces: a live URL, `make aws-up` / `make aws-down` / `make aws-smoke`.

- [ ] **Step 1: Write `scripts/aws_bootstrap_secret.py`**

```python
"""Push the local .env values into Secrets Manager.

Terraform creates the secret but never its contents, so no secret value ever lands
in Terraform state. Run this once after the first apply, and again whenever a
credential rotates.
"""
import json
import pathlib
import subprocess
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SECRET_NAME = "analytics-copilot/runtime"
REGION = "us-east-1"

# Values copied straight from .env.
FROM_ENV = [
    "ANTHROPIC_API_KEY",
    "JWT_SECRET",
    "DEMO_ANALYST_PASSWORD_HASH",
    "DEMO_ADMIN_PASSWORD_HASH",
    "SNOWFLAKE_ACCOUNT",
]


def read_env() -> dict[str, str]:
    env_path = REPO_ROOT / ".env"
    if not env_path.exists():
        sys.exit(f"No {env_path}. Nothing to upload.")
    out: dict[str, str] = {}
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def main() -> None:
    env = read_env()
    payload = {}
    for key in FROM_ENV:
        value = env.get(key, "")
        if not value or value == "dev-secret-change-me":
            sys.exit(f"{key} is missing or still the placeholder in .env. Fix it first.")
        payload[key] = value

    key_path = REPO_ROOT / env.get("SNOWFLAKE_PRIVATE_KEY_PATH", "secrets/copilot_svc_key.p8")
    if not key_path.exists():
        sys.exit(f"Snowflake private key not found at {key_path}.")
    payload["SNOWFLAKE_PRIVATE_KEY_PEM"] = key_path.read_text()

    subprocess.run(
        ["aws", "secretsmanager", "put-secret-value",
         "--secret-id", SECRET_NAME, "--region", REGION,
         "--secret-string", json.dumps(payload)],
        check=True, stdout=subprocess.DEVNULL,
    )
    print(f"Uploaded {len(payload)} keys to {SECRET_NAME}: {', '.join(sorted(payload))}")
    print("Values are not echoed. Roll the task to pick them up:")
    print("  aws ecs update-service --cluster analytics-copilot "
          "--service analytics-copilot --force-new-deployment")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Write `scripts/aws_smoke.py`**

```python
"""End-to-end check against the deployed URL.

Proves the deployment does what the local app does: login issues a JWT, an analyst
sees masked contact emails, an admin sees real ones, and unauthenticated calls are
refused.
"""
import json
import sys
import urllib.error
import urllib.request

QUESTION = "list treatment centers with their contact emails"


def post(base: str, path: str, body: dict, token: str = "") -> tuple[int, dict]:
    req = urllib.request.Request(
        f"{base}{path}", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json",
                 **({"Authorization": f"Bearer {token}"} if token else {})},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, {}


def main() -> None:
    if len(sys.argv) != 4:
        sys.exit("usage: aws_smoke.py <base-url> <analyst-password> <admin-password>")
    base, analyst_pw, admin_pw = sys.argv[1].rstrip("/"), sys.argv[2], sys.argv[3]
    failures = []

    status, _ = post(base, "/api/chat", {"question": "hi"})
    print(f"unauthenticated /api/chat -> {status} (want 401)")
    if status != 401:
        failures.append("unauthenticated request was not refused")

    seen = {}
    for role, pw in (("analyst", analyst_pw), ("admin", admin_pw)):
        status, body = post(base, "/api/auth/login", {"email": f"{role}@demo", "password": pw})
        if status != 200:
            failures.append(f"{role} login failed with {status}")
            continue
        token = body["token"]
        print(f"{role} login -> 200, role={body.get('role')}")

        status, body = post(base, "/api/chat",
                            {"question": QUESTION, "conversation_id": f"smoke-{role}"}, token)
        rows = body.get("rows") or body.get("data") or []
        flat = [str(c) for row in rows[:6] for c in (row if isinstance(row, list) else [row])]
        seen[role] = [v for v in flat if "@" in v or "MASK" in v][:3]
        print(f"{role} /api/chat -> {status} error={body.get('error_type')} "
              f"intent={body.get('intent')} emails={seen[role]}")
        if status != 200 or body.get("error_type"):
            failures.append(f"{role} chat failed: {status} {body.get('error_type')}")

    if seen.get("analyst") and not all("MASK" in v for v in seen["analyst"]):
        failures.append("analyst saw unmasked contact emails")
    if seen.get("admin") and not any("@" in v for v in seen["admin"]):
        failures.append("admin did not see real contact emails")

    print()
    if failures:
        print("SMOKE FAILED:")
        for f in failures:
            print("  -", f)
        sys.exit(1)
    print("SMOKE PASSED: auth enforced, masking differs by role, both roles answered.")


if __name__ == "__main__":
    main()
```

- [ ] **Step 3: Add Makefile targets**

Add `aws-plan aws-up aws-down aws-secret aws-smoke` to the `.PHONY` line, then append:

```make
TF := ~/.local/bin/terraform -chdir=infra

aws-plan:
	$(TF) init -input=false
	$(TF) plan

aws-up:
	$(TF) init -input=false
	$(TF) apply -auto-approve
	@echo "App URL: $$($(TF) output -raw app_url)"

aws-secret:
	$(UV) run python ../scripts/aws_bootstrap_secret.py

aws-smoke:
	@test -n "$(ANALYST_PW)" || (echo "usage: make aws-smoke ANALYST_PW=... ADMIN_PW=..." && exit 1)
	$(UV) run python ../scripts/aws_smoke.py "$$($(TF) output -raw app_url)" "$(ANALYST_PW)" "$(ADMIN_PW)"

aws-down:
	$(TF) destroy -auto-approve
```

- [ ] **Step 4: Confirm credentials, then apply**

```bash
~/.local/bin/aws sts get-caller-identity
```

If this fails, STOP and report — the operator must run `aws configure`.

Then:

```bash
cp infra/terraform.tfvars.example infra/terraform.tfvars
# edit github_repo to the real owner/name, then:
make aws-up
```

Expected: apply succeeds and prints an `app_url`. The ECS service will be running zero healthy tasks because no image exists yet — that is expected at this point.

- [ ] **Step 5: Wire the GitHub repo variables and deploy**

```bash
~/.local/bin/terraform -chdir=infra output
~/.local/bin/gh variable set AWS_DEPLOY_ROLE_ARN --body "$(~/.local/bin/terraform -chdir=infra output -raw github_deploy_role_arn)"
~/.local/bin/gh variable set WEB_BUCKET --body "$(~/.local/bin/terraform -chdir=infra output -raw web_bucket)"
~/.local/bin/gh variable set CLOUDFRONT_DISTRIBUTION_ID --body "$(~/.local/bin/terraform -chdir=infra output -raw cloudfront_distribution_id)"
```

- [ ] **Step 6: Upload the secrets and trigger the first deploy**

```bash
make aws-secret
git push                     # or: gh workflow run deploy.yml
~/.local/bin/gh run watch $(~/.local/bin/gh run list --workflow=deploy.yml --limit 1 --json databaseId --jq '.[0].databaseId')
```

Expected: the deploy job succeeds and `aws ecs wait services-stable` returns.

- [ ] **Step 7: Smoke-test the live URL**

```bash
make aws-smoke ANALYST_PW='<the analyst demo password>' ADMIN_PW='<the admin demo password>'
```

Expected: `SMOKE PASSED`. If the chat calls fail with `error_type=snowflake`, read the task logs before changing anything:

```bash
aws logs tail /ecs/analytics-copilot --since 15m --region us-east-1
```

- [ ] **Step 8: Document it**

Add a `## Deploying to AWS` section to `README.md` covering: the one-command path (`make aws-up`, `make aws-secret`, push), the architecture decisions recorded at the top of this plan (MCP in-container rather than sidecar, local Terraform state, public-subnet tasks and why), the ~$1/day cost breakdown, and `make aws-down` for teardown. State plainly that Terraform state is local and untracked, so teardown must happen from the same machine.

- [ ] **Step 9: Commit and tag**

```bash
git add scripts/aws_bootstrap_secret.py scripts/aws_smoke.py Makefile README.md
git commit -m "feat(aws): secret bootstrap, smoke test, and teardown"
git push
git tag -a v0.3-aws -m "Phase 3A: live on AWS behind CloudFront, deployed by GitHub Actions"
```

---

## Self-Review

**Spec coverage.** The Tue Aug 11 block's first three items are covered: Terraform for ECR/ECS/ALB/S3+CloudFront/Secrets Manager/IAM plus first deploy (Tasks 4–6, 8) and the GitHub Actions PR and deploy pipelines (Tasks 2, 3, 7). The spec's `/admin/*` endpoints, CloudWatch dashboards and alarms, the eval harness, and the Admin Console are deliberately Phase 3B — each is app-level work that deploys through the pipeline this plan builds. The spec's "MCP as a sidecar container over localhost HTTP" is consciously deviated from, with the reason and the scaling story recorded in the Architecture Decisions section and destined for the README.

**Placeholder scan.** Every step carries the literal file contents or commands to run. The two places a human value is required — the `github_repo` in `terraform.tfvars` and the two demo passwords for the smoke test — are marked as operator inputs rather than left as TBDs, because only the operator knows them.

**Type consistency.** `_load_private_key(path, pem="")` is defined in Task 1 and called with both arguments in the same task. `Settings.cors_origin_list()` is defined in Task 1 and consumed in Task 6's `_cors_origins()`. Terraform resource names referenced across files — `aws_ecr_repository.app`, `aws_secretsmanager_secret.app`, `aws_iam_role.ecs_execution`, `aws_iam_role.ecs_task`, `aws_s3_bucket.web`, `aws_cloudfront_distribution.web`, `aws_ecs_cluster.main`, `aws_ecs_service.app`, `aws_lb.app`, `aws_lb_target_group.app`, `aws_security_group.alb` — match between the tasks that define them (4, 5, 6) and the tasks that consume them (5, 6, 7's outputs). The cluster and service names the deploy workflow hardcodes (`analytics-copilot`) match `local.name`, which defaults to `var.project`.

**One ordering hazard worth flagging to the executor.** Task 4's IAM policy references `aws_s3_bucket.web` and `aws_cloudfront_distribution.web`, which Task 6 creates. `terraform validate` therefore cannot pass until Task 6 is done — Task 4 runs `fmt` only, and Task 6 Step 6 runs the full validate. A reviewer seeing Task 4 in isolation should not treat the missing validate as an omission.
