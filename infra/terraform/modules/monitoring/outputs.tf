output "alerts_topic_arn" {
  value = aws_sns_topic.alerts.arn
}

output "dashboard_url" {
  value = "https://console.aws.amazon.com/cloudwatch/home?region=${data.aws_region.current.region}#dashboards/dashboard/${aws_cloudwatch_dashboard.this.dashboard_name}"
}

# For tests.
output "alb_alarms" {
  value = aws_cloudwatch_metric_alarm.alb
}

output "rds_alarms" {
  value = aws_cloudwatch_metric_alarm.rds
}

output "email_subscriptions" {
  value = aws_sns_topic_subscription.email
}
