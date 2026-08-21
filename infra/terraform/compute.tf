resource "aws_cloudwatch_log_group" "webhook" {
  name              = "/aws/lambda/${local.webhook_function_name}"
  retention_in_days = var.webhook_log_retention_days
}

resource "aws_cloudwatch_log_group" "pipeline" {
  name              = "/aws/lambda/${local.pipeline_function_name}"
  retention_in_days = var.pipeline_log_retention_days
}

resource "aws_lambda_layer_version" "dependencies" {
  layer_name          = local.layer_name
  description         = "LlamaPReview Python 3.12 x86_64 runtime dependencies"
  s3_bucket           = aws_s3_bucket.artifacts.id
  s3_key              = aws_s3_object.release["layer"].key
  s3_object_version   = aws_s3_object.release["layer"].version_id
  source_code_hash    = var.release_artifacts.layer.sha256_base64
  compatible_runtimes = ["python3.12"]
  compatible_architectures = [
    "x86_64",
  ]
}

resource "aws_lambda_function" "webhook" {
  function_name = local.webhook_function_name
  description   = "Signed public-repository webhook admission for LlamaPReview"
  role          = aws_iam_role.webhook.arn
  handler       = "lambda_function.lambda_handler"
  runtime       = "python3.12"
  architectures = ["x86_64"]
  timeout       = 180
  memory_size   = 128

  s3_bucket         = aws_s3_bucket.artifacts.id
  s3_key            = aws_s3_object.release["webhook"].key
  s3_object_version = aws_s3_object.release["webhook"].version_id
  source_code_hash  = var.release_artifacts.webhook.sha256_base64
  publish           = true

  environment {
    variables = {
      DYNAMODB_PIPELINE_TABLE = aws_dynamodb_table.pipeline.name
      GITHUB_WEBHOOK_SECRET   = var.github_webhook_secret
    }
  }

  depends_on = [
    aws_cloudwatch_log_group.webhook,
    aws_iam_role_policy.webhook,
  ]
}

resource "aws_lambda_alias" "webhook" {
  name             = "LIVE"
  description      = "Immutable self-hosted Webhook release"
  function_name    = aws_lambda_function.webhook.function_name
  function_version = aws_lambda_function.webhook.version
}

resource "aws_lambda_function_url" "webhook" {
  function_name      = aws_lambda_function.webhook.function_name
  qualifier          = aws_lambda_alias.webhook.name
  authorization_type = "NONE"
  invoke_mode        = "BUFFERED"
}

resource "aws_lambda_function" "pipeline" {
  function_name = local.pipeline_function_name
  description   = "Exact-head evidence-first LlamaPReview Pipeline"
  role          = aws_iam_role.pipeline.arn
  handler       = "LlamaPReviewPipeline.lambda_function.lambda_handler"
  runtime       = "python3.12"
  architectures = ["x86_64"]
  timeout       = 900
  memory_size   = var.pipeline_memory_size
  layers        = [aws_lambda_layer_version.dependencies.arn]

  s3_bucket         = aws_s3_bucket.artifacts.id
  s3_key            = aws_s3_object.release["pipeline"].key
  s3_object_version = aws_s3_object.release["pipeline"].version_id
  source_code_hash  = var.release_artifacts.pipeline.sha256_base64
  publish           = true

  environment {
    variables = {
      ANALYZER_EFFORT                   = var.model_routing.analyzer_effort
      ANALYZER_MODEL                    = var.model_routing.analyzer_model
      CONTEXT_S3_BUCKET                 = aws_s3_bucket.artifacts.id
      DEEPSEEK_API_KEY                  = var.deepseek_api_key
      DEEPSEEK_MODEL                    = var.model_routing.deep_model
      DEEPSEEK_REASONING_EFFORT         = var.model_routing.deep_effort
      DEEPSEEK_TRACE_MODE               = var.provider_trace_mode
      DEEPSEEK_TRACE_S3_BUCKET          = aws_s3_bucket.artifacts.id
      DEEPSEEK_TRANSPORT_MODEL_OVERRIDE = var.model_routing.transport_model_override
      DRY_RUN                           = tostring(var.pipeline_dry_run)
      DYNAMODB_PIPELINE_TABLE           = aws_dynamodb_table.pipeline.name
      GITHUB_APP_ID                     = var.github_app_id
      GITHUB_PRIVATE_KEY                = var.github_private_key
      LOW_REVIEW_EFFORT                 = var.model_routing.low_review_effort
      LOW_REVIEW_MODEL                  = var.model_routing.low_review_model
      NORMAL_REVIEW_EFFORT              = var.model_routing.normal_review_effort
      NORMAL_REVIEW_MODEL               = var.model_routing.normal_review_model
      PERSIST_REVIEW_ARTIFACT           = "true"
      PFR_NORMAL_EFFORT                 = var.model_routing.normal_pfr_effort
      PFR_NORMAL_MODEL                  = var.model_routing.normal_pfr_model
      PIPELINE_CAPACITY_POLICY          = var.pipeline_capacity_policy
      PIPELINE_TTL_DAYS                 = tostring(var.pipeline_ttl_days)
      PUBLICATION_ARTIFACT_BUCKET       = aws_s3_bucket.artifacts.id
      RUN_ARTIFACT_BUCKET               = aws_s3_bucket.artifacts.id
      RUN_ARTIFACT_PREFIX               = local.runtime_prefix
      RUN_ARTIFACT_SCHEMA_VERSION       = "1"
    }
  }

  depends_on = [
    aws_cloudwatch_log_group.pipeline,
    aws_iam_role_policy.pipeline,
  ]
}

resource "aws_lambda_alias" "pipeline" {
  name             = "LIVE"
  description      = "Immutable self-hosted Pipeline release"
  function_name    = aws_lambda_function.pipeline.function_name
  function_version = aws_lambda_function.pipeline.version
}

resource "aws_lambda_event_source_mapping" "pipeline" {
  event_source_arn               = aws_dynamodb_table.pipeline.stream_arn
  function_name                  = aws_lambda_alias.pipeline.arn
  starting_position              = "LATEST"
  batch_size                     = 1
  parallelization_factor         = 1
  maximum_retry_attempts         = 2
  maximum_record_age_in_seconds  = -1
  bisect_batch_on_function_error = true
  enabled                        = var.pipeline_stream_enabled
  function_response_types        = []

  filter_criteria {
    filter {
      pattern = jsonencode({
        eventName = ["INSERT", "MODIFY"]
        dynamodb = {
          NewImage = {
            status = {
              S = ["PENDING", "CONTEXT_READY"]
            }
          }
        }
      })
    }
  }

  depends_on = [aws_iam_role_policy.pipeline]
}
