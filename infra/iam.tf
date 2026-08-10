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

# ECS Exec: lets `aws ecs execute-command` attach to a running task for
# debugging when it dies before writing anything useful to the log group.
# The task role is otherwise deliberately empty of AWS permissions (the app
# talks to Snowflake and Anthropic with its own credentials, not AWS's) --
# this is a narrow, deliberate exception scoped to exactly the four actions
# ECS Exec requires, none of which support resource-level restriction.
resource "aws_iam_role_policy" "ecs_task_exec" {
  name = "ecs-exec"
  role = aws_iam_role.ecs_task.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = [
        "ssmmessages:CreateControlChannel",
        "ssmmessages:CreateDataChannel",
        "ssmmessages:OpenControlChannel",
        "ssmmessages:OpenDataChannel",
      ]
      Resource = "*"
    }]
  })
}

# --- GitHub OIDC: short-lived credentials instead of stored access keys ---
data "tls_certificate" "github" {
  url = "https://token.actions.githubusercontent.com/.well-known/openid-configuration"
}

resource "aws_iam_openid_connect_provider" "github" {
  url            = "https://token.actions.githubusercontent.com"
  client_id_list = ["sts.amazonaws.com"]
  # The root CA is the last entry in the chain, not the first -- certificate
  # ordering has changed across tls provider major versions, and this value feeds
  # an IAM trust, so select by position rather than assuming index 0.
  thumbprint_list = [
    data.tls_certificate.github.certificates[
      length(data.tls_certificate.github.certificates) - 1
    ].sha1_fingerprint
  ]
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
        # Only this repo, and only from main -- a fork's pull_request workflow runs
        # with a different sub and cannot assume this role, and a wildcard branch
        # match would let any pushed branch with an `id-token: write` workflow mint
        # deploy credentials even though the deploy workflow only fires on
        # push-to-main and workflow_dispatch. NOTE: adding an `environment:` to the
        # deploy job changes the subject claim to
        # repo:${var.github_repo}:environment:<name> and would break this assume --
        # update the condition to match if that's ever introduced.
        #
        # Two accepted subjects, not one. GitHub now issues an *immutable* subject
        # claim that embeds numeric owner and repo ids
        # (repo:owner@1234/repo@5678:ref:...), so a rename cannot silently transfer
        # trust to whoever claims the old name. Every OIDC guide written before that
        # feature -- and the first version of this policy -- matches only the classic
        # form, which fails with a bare "Not authorized to perform
        # sts:AssumeRoleWithWebIdentity" that names neither claim. Check which form a
        # repo issues with:
        #   gh api repos/<owner>/<repo>/actions/oidc/customization/sub
        # A list under StringEquals is OR, and both entries are exact -- no wildcard,
        # so this stays scoped to this one repo on main.
        StringEquals = {
          "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
          "token.actions.githubusercontent.com:sub" = [
            "repo:${var.github_repo}:ref:refs/heads/main",
            "repo:${var.github_repo_immutable}:ref:refs/heads/main",
          ]
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
        # Scoped to this one service -- `aws ecs wait services-stable` (called by
        # the deploy pipeline after UpdateService) only needs DescribeServices on
        # this service, so scoping does not break the pipeline.
        Effect   = "Allow"
        Action   = ["ecs:UpdateService", "ecs:DescribeServices"]
        Resource = [aws_ecs_service.app.id]
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
