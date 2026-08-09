resource "aws_cloudwatch_metric_alarm" "webhook_application_errors" {
  alarm_name          = "${local.webhook_function_name}-application-errors"
  alarm_description   = "Signed Webhook admission reported an application error."
  namespace           = "LlamaPReview/Webhook"
  metric_name         = "Errors"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = var.alarm_actions
}

resource "aws_cloudwatch_metric_alarm" "webhook_errors" {
  alarm_name          = "${local.webhook_function_name}-errors"
  alarm_description   = "Webhook Lambda invocation errors exceeded zero."
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = var.alarm_actions

  dimensions = {
    FunctionName = aws_lambda_function.webhook.function_name
    Resource     = "${aws_lambda_function.webhook.function_name}:${aws_lambda_alias.webhook.name}"
  }
}

resource "aws_cloudwatch_metric_alarm" "pipeline_errors" {
  alarm_name          = "${local.pipeline_function_name}-errors"
  alarm_description   = "Pipeline Lambda invocation errors exceeded zero."
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = var.alarm_actions

  dimensions = {
    FunctionName = aws_lambda_function.pipeline.function_name
    Resource     = "${aws_lambda_function.pipeline.function_name}:${aws_lambda_alias.pipeline.name}"
  }
}

resource "aws_cloudwatch_metric_alarm" "pipeline_terminal_errors" {
  alarm_name          = "${local.pipeline_function_name}-terminal-errors"
  alarm_description   = "Pipeline reported an unexpected durable terminal error."
  namespace           = "LlamaPReview/Pipeline"
  metric_name         = "TerminalErrors"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = var.alarm_actions
}

resource "aws_cloudwatch_metric_alarm" "pipeline_iterator_age" {
  alarm_name          = "${local.pipeline_function_name}-iterator-age"
  alarm_description   = "Pipeline DynamoDB Stream backlog exceeded 30 minutes."
  namespace           = "AWS/Lambda"
  metric_name         = "IteratorAge"
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1800000
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = var.alarm_actions

  dimensions = {
    FunctionName = aws_lambda_function.pipeline.function_name
    Resource     = "${aws_lambda_function.pipeline.function_name}:${aws_lambda_alias.pipeline.name}"
  }
}
