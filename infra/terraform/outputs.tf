output "webhook_function_url" {
  description = "Alias-qualified public URL to configure as the GitHub App webhook endpoint."
  value       = aws_lambda_function_url.webhook.function_url
}

output "webhook_release" {
  description = "Immutable Webhook alias and artifact identity."
  value = {
    function_name   = aws_lambda_function.webhook.function_name
    alias           = aws_lambda_alias.webhook.name
    numeric_version = aws_lambda_function.webhook.version
    artifact_key    = aws_s3_object.release["webhook"].key
    sha256_hex      = lower(var.release_artifacts.webhook.sha256_hex)
    sha256_base64   = var.release_artifacts.webhook.sha256_base64
  }
}

output "pipeline_release" {
  description = "Immutable Pipeline alias, Layer, artifact, and ESM identity."
  value = {
    function_name   = aws_lambda_function.pipeline.function_name
    alias           = aws_lambda_alias.pipeline.name
    numeric_version = aws_lambda_function.pipeline.version
    artifact_key    = aws_s3_object.release["pipeline"].key
    sha256_hex      = lower(var.release_artifacts.pipeline.sha256_hex)
    sha256_base64   = var.release_artifacts.pipeline.sha256_base64
    layer_version   = aws_lambda_layer_version.dependencies.version
    layer_arn       = aws_lambda_layer_version.dependencies.arn
    layer_sha256    = lower(var.release_artifacts.layer.sha256_hex)
    esm_uuid        = aws_lambda_event_source_mapping.pipeline.uuid
    esm_enabled     = var.pipeline_stream_enabled
    dry_run         = var.pipeline_dry_run
  }
}

output "private_storage" {
  description = "Private state and artifact resources created for this deployment."
  value = {
    table_name                = aws_dynamodb_table.pipeline.name
    artifact_bucket_name      = aws_s3_bucket.artifacts.id
    pipeline_ttl_days         = var.pipeline_ttl_days
    artifact_retention_days   = var.artifact_retention_days
    full_trace_retention_days = var.full_trace_retention_days
    trace_mode                = var.provider_trace_mode
  }
}

output "alarm_names" {
  description = "CloudWatch alarms that require operator monitoring or optional SNS actions."
  value = [
    aws_cloudwatch_metric_alarm.webhook_application_errors.alarm_name,
    aws_cloudwatch_metric_alarm.webhook_errors.alarm_name,
    aws_cloudwatch_metric_alarm.pipeline_errors.alarm_name,
    aws_cloudwatch_metric_alarm.pipeline_terminal_errors.alarm_name,
    aws_cloudwatch_metric_alarm.pipeline_iterator_age.alarm_name,
  ]
}
