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
