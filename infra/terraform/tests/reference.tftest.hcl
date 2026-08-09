mock_provider "aws" {
  mock_data "aws_iam_policy_document" {
    defaults = {
      json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}"
    }
  }

  mock_resource "aws_s3_bucket" {
    defaults = {
      arn    = "arn:aws:s3:::mock-llamapreview-artifacts"
      bucket = "mock-llamapreview-artifacts"
      id     = "mock-llamapreview-artifacts"
    }
  }

  mock_resource "aws_s3_object" {
    defaults = {
      version_id = "mock-version"
    }
  }

  mock_resource "aws_dynamodb_table" {
    defaults = {
      arn        = "arn:aws:dynamodb:us-east-1:111111111111:table/mock-runs"
      stream_arn = "arn:aws:dynamodb:us-east-1:111111111111:table/mock-runs/stream/mock"
    }
  }

  mock_resource "aws_iam_role" {
    defaults = {
      arn = "arn:aws:iam::111111111111:role/mock-lambda-role"
    }
  }

  mock_resource "aws_cloudwatch_log_group" {
    defaults = {
      arn = "arn:aws:logs:us-east-1:111111111111:log-group:mock"
    }
  }

  mock_resource "aws_lambda_layer_version" {
    defaults = {
      arn     = "arn:aws:lambda:us-east-1:111111111111:layer:mock:1"
      version = 1
    }
  }

  mock_resource "aws_lambda_function" {
    defaults = {
      arn     = "arn:aws:lambda:us-east-1:111111111111:function:mock"
      version = "1"
    }
  }

  mock_resource "aws_lambda_alias" {
    defaults = {
      arn = "arn:aws:lambda:us-east-1:111111111111:function:mock:LIVE"
    }
  }
}

run "safe_reference_topology" {
  command = apply

  variables {
    release_artifacts = {
      webhook = {
        path          = "tests/fixtures/mock-artifact.txt"
        sha256_hex    = "4f2bb102766ce78912ffb8242506723583375c24b919ea6eb28b33138e24bfc2"
        sha256_base64 = "TyuxAnZs54kS/7gkJQZyNYM3XCS5GepusoszE44kv8I="
      }
      pipeline = {
        path          = "tests/fixtures/mock-artifact.txt"
        sha256_hex    = "4f2bb102766ce78912ffb8242506723583375c24b919ea6eb28b33138e24bfc2"
        sha256_base64 = "TyuxAnZs54kS/7gkJQZyNYM3XCS5GepusoszE44kv8I="
      }
      layer = {
        path          = "tests/fixtures/mock-artifact.txt"
        sha256_hex    = "4f2bb102766ce78912ffb8242506723583375c24b919ea6eb28b33138e24bfc2"
        sha256_base64 = "TyuxAnZs54kS/7gkJQZyNYM3XCS5GepusoszE44kv8I="
      }
    }

    github_app_id         = "123456"
    github_private_key    = "mock-private-key"
    github_webhook_secret = "mock-webhook-secret-long-value"
    deepseek_api_key      = "mock-provider-key"
  }

  assert {
    condition = (
      aws_lambda_alias.webhook.function_version == aws_lambda_function.webhook.version &&
      aws_lambda_alias.pipeline.function_version == aws_lambda_function.pipeline.version
    )
    error_message = "Both public entrypoints must use aliases targeting published immutable versions."
  }

  assert {
    condition = (
      aws_lambda_function_url.webhook.qualifier == "LIVE" &&
      aws_lambda_function_url.webhook.authorization_type == "NONE"
    )
    error_message = "The signed GitHub endpoint must be the immutable Webhook LIVE alias."
  }

  assert {
    condition = (
      aws_lambda_event_source_mapping.pipeline.function_name == aws_lambda_alias.pipeline.arn &&
      aws_lambda_event_source_mapping.pipeline.enabled == false
    )
    error_message = "The Stream must target Pipeline LIVE and remain disabled on the safe first plan."
  }

  assert {
    condition = (
      aws_dynamodb_table.pipeline.stream_enabled &&
      aws_dynamodb_table.pipeline.stream_view_type == "NEW_AND_OLD_IMAGES" &&
      aws_dynamodb_table.pipeline.billing_mode == "PAY_PER_REQUEST"
    )
    error_message = "The single durable table must expose the exact Stream contract."
  }

  assert {
    condition = (
      aws_s3_bucket_public_access_block.artifacts.block_public_acls &&
      aws_s3_bucket_public_access_block.artifacts.block_public_policy &&
      aws_s3_bucket_public_access_block.artifacts.ignore_public_acls &&
      aws_s3_bucket_public_access_block.artifacts.restrict_public_buckets
    )
    error_message = "The release/runtime artifact bucket must be private."
  }

  assert {
    condition = (
      aws_lambda_function.pipeline.environment[0].variables["DRY_RUN"] == "true" &&
      aws_lambda_function.pipeline.environment[0].variables["PERSIST_REVIEW_ARTIFACT"] == "true"
    )
    error_message = "The first plan must preserve zero-write validation and durable recovery artifacts."
  }
}

run "explicit_activation" {
  command = plan

  variables {
    release_artifacts = {
      webhook = {
        path          = "tests/fixtures/mock-artifact.txt"
        sha256_hex    = "4f2bb102766ce78912ffb8242506723583375c24b919ea6eb28b33138e24bfc2"
        sha256_base64 = "TyuxAnZs54kS/7gkJQZyNYM3XCS5GepusoszE44kv8I="
      }
      pipeline = {
        path          = "tests/fixtures/mock-artifact.txt"
        sha256_hex    = "4f2bb102766ce78912ffb8242506723583375c24b919ea6eb28b33138e24bfc2"
        sha256_base64 = "TyuxAnZs54kS/7gkJQZyNYM3XCS5GepusoszE44kv8I="
      }
      layer = {
        path          = "tests/fixtures/mock-artifact.txt"
        sha256_hex    = "4f2bb102766ce78912ffb8242506723583375c24b919ea6eb28b33138e24bfc2"
        sha256_base64 = "TyuxAnZs54kS/7gkJQZyNYM3XCS5GepusoszE44kv8I="
      }
    }

    github_app_id           = "123456"
    github_private_key      = "mock-private-key"
    github_webhook_secret   = "mock-webhook-secret-long-value"
    deepseek_api_key        = "mock-provider-key"
    pipeline_dry_run        = false
    pipeline_stream_enabled = true
  }

  assert {
    condition = (
      aws_lambda_event_source_mapping.pipeline.enabled &&
      aws_lambda_function.pipeline.environment[0].variables["DRY_RUN"] == "false"
    )
    error_message = "Live publication requires both explicit activation inputs."
  }
}
