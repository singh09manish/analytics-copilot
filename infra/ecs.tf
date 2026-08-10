resource "aws_cloudwatch_log_group" "app" {
  name              = "/ecs/${local.name}"
  retention_in_days = 7 # demo retention; longer is pure cost here
}

resource "aws_ecs_cluster" "main" {
  name = local.name

  # Container Insights bills per-metric. This is a $1-2/day demo stack, so pin
  # it off explicitly rather than inherit whatever the account default is --
  # if that default is ever on (or "enhanced"), the CloudWatch bill can exceed
  # the rest of the stack combined.
  setting {
    name  = "containerInsights"
    value = "disabled"
  }
}

resource "aws_security_group" "task" {
  name_prefix = "${local.name}-task-"
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

  # name_prefix (not a fixed name) so a future replacement can create the new
  # group before destroying the old one instead of failing on
  # InvalidGroup.Duplicate.
  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_ecs_task_definition" "app" {
  family                   = local.name
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 512
  # The task runs uvicorn plus langgraph/langchain/anthropic/snowflake-connector,
  # AND a second full Python interpreter -- the MCP subprocess, spawned with
  # the parent env, importing its own copy of snowflake-connector. 1024 MiB
  # OOM-kills under that combined footprint with a bare exit 137 and no
  # application log line, which is an expensive thing to debug on demo day.
  # +$0.11/day keeps this inside the $1-2/day target; cpu stays at 512.
  memory             = 2048
  execution_role_arn = aws_iam_role.ecs_execution.arn
  task_role_arn      = aws_iam_role.ecs_task.arn

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

  # Importing langgraph/langchain/anthropic/snowflake-connector before uvicorn
  # binds plausibly takes 20-40s on 0.5 vCPU. Without a grace period the
  # scheduler evaluates ALB health the instant the task registers (2x30s to
  # pass, 3x30s to be killed by the target group), which turns a slow cold
  # start or image pull into a kill-and-restart loop that reads as an
  # application bug.
  health_check_grace_period_seconds = 120

  # Lets `aws ecs execute-command` attach to a running task for debugging --
  # useful when a task dies before it manages to write anything to logs.
  enable_execute_command = true

  network_configuration {
    subnets         = data.aws_subnets.app.ids
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

  # Without this, a crash-looping task makes `aws ecs wait services-stable`
  # hang until its own ~10 minute timeout and fail with no rollback. This
  # fails fast and reverts to the last working revision instead.
  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  # CI deploys by forcing a new deployment of the same task definition
  # revision (`--force-new-deployment`, never `register-task-definition`), and
  # Fargate always re-pulls the `:latest` tag on redeploy -- so Terraform
  # still owns the task definition and must NOT ignore drift on it. Ignoring
  # it would silently no-op any future change to var.app_model, CORS origins,
  # cpu/memory, or a new secret key: the apply would appear to succeed and do
  # nothing. desired_count is still ignored so a manual scale (e.g. bumping
  # replicas for a demo) isn't reverted by the next apply.
  lifecycle {
    ignore_changes = [desired_count]
  }

  depends_on = [aws_lb_listener.http]
}
