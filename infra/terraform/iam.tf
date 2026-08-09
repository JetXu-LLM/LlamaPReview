data "aws_iam_policy_document" "lambda_trust" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "webhook" {
  name               = "${local.webhook_function_name}-role"
  assume_role_policy = data.aws_iam_policy_document.lambda_trust.json
}

data "aws_iam_policy_document" "webhook" {
  statement {
    sid = "WriteOnlyAdmittedPipelineItems"
    actions = [
      "dynamodb:DescribeTable",
      "dynamodb:PutItem",
    ]
    resources = [aws_dynamodb_table.pipeline.arn]
  }

  statement {
    sid = "WriteWebhookLogs"
    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]
    resources = ["${aws_cloudwatch_log_group.webhook.arn}:*"]
  }
}

resource "aws_iam_role_policy" "webhook" {
  name   = "${local.webhook_function_name}-runtime"
  role   = aws_iam_role.webhook.id
  policy = data.aws_iam_policy_document.webhook.json
}

resource "aws_iam_role" "pipeline" {
  name               = "${local.pipeline_function_name}-role"
  assume_role_policy = data.aws_iam_policy_document.lambda_trust.json
}

data "aws_iam_policy_document" "pipeline" {
  statement {
    sid = "ReadAndUpdatePipelineState"
    actions = [
      "dynamodb:DescribeTable",
      "dynamodb:GetItem",
      "dynamodb:UpdateItem",
    ]
    resources = [aws_dynamodb_table.pipeline.arn]
  }

  statement {
    sid = "ConsumeExactPipelineStream"
    actions = [
      "dynamodb:DescribeStream",
      "dynamodb:GetRecords",
      "dynamodb:GetShardIterator",
    ]
    resources = [aws_dynamodb_table.pipeline.stream_arn]
  }

  # DynamoDB ListStreams does not support resource-level permissions.
  statement {
    sid       = "DiscoverDynamoDBStreams"
    actions   = ["dynamodb:ListStreams"]
    resources = ["*"]
  }

  statement {
    sid = "ReadAndWritePrivateRuntimeArtifacts"
    actions = [
      "s3:GetObject",
      "s3:PutObject",
    ]
    resources = ["${aws_s3_bucket.artifacts.arn}/${local.runtime_prefix}/*"]
  }

  statement {
    sid = "WritePipelineLogs"
    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]
    resources = ["${aws_cloudwatch_log_group.pipeline.arn}:*"]
  }
}

resource "aws_iam_role_policy" "pipeline" {
  name   = "${local.pipeline_function_name}-runtime"
  role   = aws_iam_role.pipeline.id
  policy = data.aws_iam_policy_document.pipeline.json
}
