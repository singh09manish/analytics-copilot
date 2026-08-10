# Fargate cannot place tasks in the default VPC's use1-az3 subnet, and the AZ
# *letter* mapped to that AZ id is randomized per AWS account, so subnets must
# be selected by AZ id, not by name. EC2's DescribeSubnets filters have no
# negation operator, so this enumerates every AZ id available in the region
# except use1-az3 and filters the default VPC's subnets down to that set. Both
# the ALB and the ECS service use this instead of data.aws_subnets.default.
data "aws_availability_zones" "usable" {
  state = "available"
}

data "aws_subnets" "app" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }

  filter {
    name   = "availability-zone-id"
    values = setsubtract(data.aws_availability_zones.usable.zone_ids, ["use1-az3"])
  }
}

# AWS-managed list of CloudFront's origin-facing IP ranges. Restricting ALB
# ingress to this list -- instead of 0.0.0.0/0 -- is what makes CloudFront the
# only path in; without it, anyone can reach the API origin directly over
# plain HTTP, sidestepping the HTTPS boundary CloudFront exists to enforce.
data "aws_ec2_managed_prefix_list" "cloudfront" {
  name = "com.amazonaws.global.cloudfront.origin-facing"
}

resource "aws_security_group" "alb" {
  name_prefix = "${local.name}-alb-"
  description = "Public ingress to the ALB, restricted to CloudFront's origin-facing ranges."
  vpc_id      = data.aws_vpc.default.id

  ingress {
    description     = "HTTP from CloudFront only (CloudFront terminates HTTPS for this origin)"
    from_port       = 80
    to_port         = 80
    protocol        = "tcp"
    prefix_list_ids = [data.aws_ec2_managed_prefix_list.cloudfront.id]
  }

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

resource "aws_lb" "app" {
  name               = "${local.name}-alb"
  load_balancer_type = "application"
  security_groups    = [aws_security_group.alb.id]
  subnets            = data.aws_subnets.app.ids

  # /chat is a non-streaming JSON endpoint, so the ALB counts the whole
  # request as idle until the response byte arrives. The pipeline makes 3-4
  # sequential Claude calls around one or two Snowflake executions, and
  # McpExecutor.run_query alone carries a 60s budget the repair edge can spend
  # twice -- the plan's own smoke test uses a 120s client timeout, which is an
  # admission that the 60s default isn't enough. At the default, the ALB
  # returns 504 while the task is still working.
  idle_timeout = 180
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
