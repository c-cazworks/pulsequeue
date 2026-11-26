# AWS-side alerting from metrics AWS already collects, so it needs no pods in the cluster.
# App-level metrics (queue depth, worker failures) are covered by Prometheus: deploy/observability.

data "aws_region" "current" {}

locals {
  # The ALB is created by the load balancer controller, so its ID is unknown here and changes on
  # every redeploy. Metrics Insights queries aggregate across the project's ALBs instead; this
  # project has one.
  alb = "SCHEMA(\"AWS/ApplicationELB\", LoadBalancer)"
  tg  = "SCHEMA(\"AWS/ApplicationELB\", LoadBalancer, TargetGroup)"

  alb_alarms = {
    target-5xx = {
      description = "The app returned 5xx responses through the ALB. Runbook: API errors or API down."
      query       = "SELECT SUM(HTTPCode_Target_5XX_Count) FROM ${local.alb}"
      threshold   = 10
      periods     = 1
    }
    # The ALB answers 502/503 itself when no target is healthy: the site is down.
    alb-5xx = {
      description = "The ALB returned 5xx itself, usually because no target is healthy. Runbook: ALB has no healthy targets."
      query       = "SELECT SUM(HTTPCode_ELB_5XX_Count) FROM ${local.alb}"
      threshold   = 5
      periods     = 1
    }
    # Metrics Insights has no percentiles, so this is the average, not p95.
    slow = {
      description = "Average response time above 500 ms for 15 minutes. Runbook: API slow."
      query       = "SELECT AVG(TargetResponseTime) FROM ${local.alb}"
      threshold   = 0.5
      periods     = 3
    }
    unhealthy-targets = {
      description = "At least one pod failed ALB health checks for 15 minutes. Runbook: ALB has no healthy targets."
      query       = "SELECT MAX(UnHealthyHostCount) FROM ${local.tg}"
      threshold   = 0
      periods     = 3
    }
  }

  rds_alarms = {
    cpu = {
      description = "Database CPU above 80% for 15 minutes. Runbook: Database under pressure."
      metric      = "CPUUtilization"
      statistic   = "Average"
      operator    = "GreaterThanThreshold"
      threshold   = 80
    }
    # db.t4g.micro allows about 80 connections; the app pools need at most 4 x 10 + 2 x 2.
    connections = {
      description = "More than 60 database connections. Runbook: Database under pressure."
      metric      = "DatabaseConnections"
      statistic   = "Maximum"
      operator    = "GreaterThanThreshold"
      threshold   = 60
    }
    storage = {
      description = "Less than 2 GiB of database storage left. Runbook: Database under pressure."
      metric      = "FreeStorageSpace"
      statistic   = "Minimum"
      operator    = "LessThanThreshold"
      threshold   = 2 * 1024 * 1024 * 1024
    }
  }
}

# Not KMS-encrypted: CloudWatch cannot publish to a topic that uses the AWS-managed SNS key,
# and alerts carry no secrets.
resource "aws_sns_topic" "alerts" {
  name = "${var.name}-alerts"
}

# Email subscriptions stay pending until the link AWS sends is clicked.
resource "aws_sns_topic_subscription" "email" {
  count = var.alert_email == null ? 0 : 1

  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

resource "aws_cloudwatch_metric_alarm" "alb" {
  for_each = local.alb_alarms

  alarm_name          = "${var.name}-alb-${each.key}"
  alarm_description   = each.value.description
  comparison_operator = "GreaterThanThreshold"
  threshold           = each.value.threshold
  evaluation_periods  = each.value.periods
  datapoints_to_alarm = each.value.periods
  # No ALB exists until the app is deployed; that is not an outage.
  treat_missing_data = "notBreaching"
  alarm_actions      = [aws_sns_topic.alerts.arn]
  ok_actions         = [aws_sns_topic.alerts.arn]

  metric_query {
    id          = "q"
    expression  = each.value.query
    period      = 300
    return_data = true
  }
}

resource "aws_cloudwatch_metric_alarm" "rds" {
  for_each = local.rds_alarms

  alarm_name          = "${var.name}-rds-${each.key}"
  alarm_description   = each.value.description
  namespace           = "AWS/RDS"
  metric_name         = each.value.metric
  dimensions          = { DBInstanceIdentifier = var.db_instance_id }
  statistic           = each.value.statistic
  comparison_operator = each.value.operator
  threshold           = each.value.threshold
  period              = 300
  evaluation_periods  = 3
  datapoints_to_alarm = 3
  treat_missing_data  = "missing"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  ok_actions          = [aws_sns_topic.alerts.arn]
}

resource "aws_cloudwatch_dashboard" "this" {
  dashboard_name = var.name

  dashboard_body = jsonencode({
    widgets = concat(
      # One query per widget: CloudWatch runs at most one Metrics Insights query per request.
      [for i, w in [
        { title = "ALB requests", q = "SELECT SUM(RequestCount) FROM ${local.alb}" },
        { title = "Average response time (s)", q = "SELECT AVG(TargetResponseTime) FROM ${local.alb}" },
        { title = "5xx from the app", q = "SELECT SUM(HTTPCode_Target_5XX_Count) FROM ${local.alb}" },
        { title = "5xx from the ALB (no healthy target)", q = "SELECT SUM(HTTPCode_ELB_5XX_Count) FROM ${local.alb}" },
        { title = "Healthy targets", q = "SELECT MIN(HealthyHostCount) FROM ${local.tg}" },
        { title = "Unhealthy targets", q = "SELECT MAX(UnHealthyHostCount) FROM ${local.tg}" },
        ] : {
        type   = "metric"
        x      = (i % 2) * 12
        y      = floor(i / 2) * 6
        width  = 12
        height = 6
        properties = {
          title   = w.title
          region  = data.aws_region.current.region
          view    = "timeSeries"
          period  = 60
          metrics = [[{ expression = w.q, id = "q", label = w.title }]]
        }
      }],
      [for i, m in ["CPUUtilization", "DatabaseConnections", "FreeStorageSpace", "FreeableMemory"] : {
        type   = "metric"
        x      = (i % 2) * 12
        y      = 18 + floor(i / 2) * 6
        width  = 12
        height = 6
        properties = {
          title   = "RDS ${m}"
          region  = data.aws_region.current.region
          view    = "timeSeries"
          period  = 60
          stat    = "Average"
          metrics = [["AWS/RDS", m, "DBInstanceIdentifier", var.db_instance_id]]
        }
      }],
      [{
        type   = "alarm"
        x      = 0
        y      = 30
        width  = 24
        height = 4
        properties = {
          title  = "Alarms"
          alarms = concat([for a in aws_cloudwatch_metric_alarm.alb : a.arn], [for a in aws_cloudwatch_metric_alarm.rds : a.arn])
        }
      }]
    )
  })
}
