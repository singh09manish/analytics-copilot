# Dashboard and alarms over the EMF metrics the app already emits -- see
# backend/src/copilot/metrics.py (the `_aws` block format) and the emission block
# in backend/src/copilot/api/main.py (the actual emit() call sites). CloudWatch
# extracts EMF metrics from that `_aws` block automatically the moment a matching
# log line lands in aws_cloudwatch_log_group.app (declared in ecs.tf); there is no
# aws_cloudwatch_log_metric_filter here because none is needed.
#
# Every metric name and dimension below is copied from the emit() call sites, not
# guessed -- a widget naming a dimension that is never emitted renders empty
# forever with no error:
#   Answered      (role, outcome, intent)  -- main.py, in the /api/chat handler
#   LatencyMs     (role, outcome)          -- main.py, same block
#   RetrievalMs   (role, mode)             -- main.py, same block
#   TokensTotal   (role)                   -- main.py, same block
# outcome is exactly one of "ok", "validation", "llm", "snowflake" (the only
# values agent/pipeline.py and agent/graph.py ever assign to ChatResponse.error_type,
# with "ok" standing in for None). mode is exactly "vector" or "keyword"
# (retrieval.py's RetrievedContext.mode).
#
# Answered's three dimensions (role, outcome, intent) are emitted together as one
# dimension set, not one each -- so a widget that wants "per outcome" cannot just
# name Namespace/MetricName/outcome the way a single-dimension metric would; it has
# to use a metrics-search expression that pins outcome and lets role/intent vary,
# then collapses the resulting series with SUM()/AVG(). CloudWatch explicitly
# supports alarming and graphing on a math expression built this way (SUM(SEARCH(...))
# etc.), which is what every widget and the error_rate alarm below do.

resource "aws_cloudwatch_dashboard" "main" {
  dashboard_name = "${local.name}-overview"

  dashboard_body = jsonencode({
    widgets = [
      # --- Answered / min, by outcome -----------------------------------
      {
        type = "metric", x = 0, y = 0, width = 12, height = 6
        properties = {
          title   = "Answered / min by outcome"
          view    = "timeSeries"
          stacked = true
          region  = var.region
          period  = 60
          metrics = [
            [{
              expression = "SUM(SEARCH('{AnalyticsCopilot,role,outcome,intent} MetricName=\"Answered\" outcome=\"ok\"', 'Sum', 60))"
              id         = "e1", label = "ok"
            }],
            [{
              expression = "SUM(SEARCH('{AnalyticsCopilot,role,outcome,intent} MetricName=\"Answered\" outcome=\"validation\"', 'Sum', 60))"
              id         = "e2", label = "validation"
            }],
            [{
              expression = "SUM(SEARCH('{AnalyticsCopilot,role,outcome,intent} MetricName=\"Answered\" outcome=\"llm\"', 'Sum', 60))"
              id         = "e3", label = "llm"
            }],
            [{
              expression = "SUM(SEARCH('{AnalyticsCopilot,role,outcome,intent} MetricName=\"Answered\" outcome=\"snowflake\"', 'Sum', 60))"
              id         = "e4", label = "snowflake"
            }],
          ]
        }
      },
      # --- p50 / p95 latency ---------------------------------------------
      {
        type = "metric", x = 12, y = 0, width = 12, height = 6
        properties = {
          title  = "Latency p50 / p95 (ms)"
          view   = "timeSeries"
          region = var.region
          period = 60
          # Percentiles cannot be summed across series, so each expression is left
          # as a raw SEARCH (one line per role x outcome combination actually
          # emitted) rather than wrapped in SUM()/AVG() like the count-based
          # widgets below.
          metrics = [
            [{
              expression = "SEARCH('{AnalyticsCopilot,role,outcome} MetricName=\"LatencyMs\"', 'p50', 60)"
              id         = "l1", label = "p50"
            }],
            [{
              expression = "SEARCH('{AnalyticsCopilot,role,outcome} MetricName=\"LatencyMs\"', 'p95', 60)"
              id         = "l2", label = "p95"
            }],
          ]
        }
      },
      # --- Retrieval latency by mode --------------------------------------
      {
        type = "metric", x = 0, y = 6, width = 12, height = 6
        properties = {
          title  = "Retrieval latency (ms) by mode"
          view   = "timeSeries"
          region = var.region
          period = 60
          metrics = [
            [{
              expression = "AVG(SEARCH('{AnalyticsCopilot,role,mode} MetricName=\"RetrievalMs\" mode=\"vector\"', 'Average', 60))"
              id         = "r1", label = "vector"
            }],
            [{
              expression = "AVG(SEARCH('{AnalyticsCopilot,role,mode} MetricName=\"RetrievalMs\" mode=\"keyword\"', 'Average', 60))"
              id         = "r2", label = "keyword"
            }],
          ]
        }
      },
      # --- Tokens / min ----------------------------------------------------
      {
        type = "metric", x = 12, y = 6, width = 12, height = 6
        properties = {
          title  = "Tokens / min (in + out)"
          view   = "timeSeries"
          region = var.region
          period = 60
          metrics = [
            [{
              expression = "SUM(SEARCH('{AnalyticsCopilot,role} MetricName=\"TokensTotal\"', 'Sum', 60))"
              id         = "t1", label = "tokens/min"
            }],
          ]
        }
      },
      # --- ECS service CPU / memory ----------------------------------------
      {
        type = "metric", x = 0, y = 12, width = 12, height = 6
        properties = {
          title  = "ECS service CPU / memory (%)"
          view   = "timeSeries"
          region = var.region
          period = 60
          metrics = [
            ["AWS/ECS", "CPUUtilization", "ClusterName", aws_ecs_cluster.main.name,
            "ServiceName", aws_ecs_service.app.name, { stat = "Average", label = "CPU %" }],
            ["AWS/ECS", "MemoryUtilization", "ClusterName", aws_ecs_cluster.main.name,
            "ServiceName", aws_ecs_service.app.name, { stat = "Average", label = "Memory %" }],
          ]
        }
      },
      # --- Recent non-ok answers, straight from the log group -------------
      # The one widget that reads the log group directly rather than the metrics
      # CloudWatch already extracted from it -- useful for "what actually broke"
      # in a way a count on a graph cannot show.
      {
        type = "log", x = 12, y = 12, width = 12, height = 6
        properties = {
          title  = "Recent non-ok answers"
          view   = "table"
          region = var.region
          query  = <<-EOQ
            SOURCE '${aws_cloudwatch_log_group.app.name}'
            | fields @timestamp, outcome, intent, role
            | filter outcome != "ok"
            | sort @timestamp desc
            | limit 20
          EOQ
        }
      },
    ]
  })
}

# --- Alarms ------------------------------------------------------------------
#
# aws_cloudwatch_metric_alarm.no_traffic is deliberately NOT defined here. This
# is a $1-2/day demo stack with no steady traffic -- zero requests for hours is
# the normal state, not an incident -- so an alarm on "no Answered in N minutes"
# would page constantly and train everyone to ignore it. If this ever becomes a
# real service with an SLO, add it back with a schedule-aware threshold instead
# of a flat one.

# Non-ok Answered count, sustained over two consecutive 5-minute periods (not a
# single blip -- one bad request during a model hiccup is not a regression).
# Built as total-minus-ok rather than a direct "outcome != ok" filter because
# CloudWatch's metric search syntax only matches exact dimension values, not
# inequality; subtracting the "ok" count from the total is the metric-math
# equivalent of "!=".
resource "aws_cloudwatch_metric_alarm" "error_rate" {
  alarm_name          = "${local.name}-error-rate"
  alarm_description   = "More than 3 non-ok Answered results in a 5-minute window, twice in a row."
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  datapoints_to_alarm = 2
  threshold           = 3
  # The stack is idle most of the time; missing data is the normal state, not a
  # breach. Applies to every alarm in this file.
  treat_missing_data = "notBreaching"

  metric_query {
    id          = "total"
    expression  = "SUM(SEARCH('{AnalyticsCopilot,role,outcome,intent} MetricName=\"Answered\"', 'Sum', 300))"
    label       = "Total answered"
    return_data = false
  }

  metric_query {
    id          = "ok"
    expression  = "SUM(SEARCH('{AnalyticsCopilot,role,outcome,intent} MetricName=\"Answered\" outcome=\"ok\"', 'Sum', 300))"
    label       = "OK answered"
    return_data = false
  }

  metric_query {
    id          = "errors"
    expression  = "total - ok"
    label       = "Non-ok answered"
    return_data = true
  }
}

# A task that dies (OOM, crash-loop, failed health check) is the failure that
# actually takes the demo down -- unlike error_rate, this needs no metric math:
# UnHealthyHostCount is already a single well-dimensioned metric.
resource "aws_cloudwatch_metric_alarm" "unhealthy_hosts" {
  alarm_name          = "${local.name}-unhealthy-hosts"
  alarm_description   = "At least one unhealthy target behind the ALB for two consecutive minutes."
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  datapoints_to_alarm = 2
  threshold           = 0
  treat_missing_data  = "notBreaching"

  namespace   = "AWS/ApplicationELB"
  metric_name = "UnHealthyHostCount"
  period      = 60
  statistic   = "Maximum"
  dimensions = {
    TargetGroup  = aws_lb_target_group.app.arn_suffix
    LoadBalancer = aws_lb.app.arn_suffix
  }
}
