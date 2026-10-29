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

variables {
  release_artifacts = {
    webhook = {
      path          = "tests/fixtures/mock-artifact.txt"
      sha256_hex    = "19688284c4266ed2805347de8445d203fede7a35412ac593d2ab077c0316a82f"
      sha256_base64 = "GWiChMQmbtKAU0fehEXSA/7eejVBKsWT0qsHfAMWqC8="
    }
    pipeline = {
      path          = "tests/fixtures/mock-artifact.txt"
      sha256_hex    = "19688284c4266ed2805347de8445d203fede7a35412ac593d2ab077c0316a82f"
      sha256_base64 = "GWiChMQmbtKAU0fehEXSA/7eejVBKsWT0qsHfAMWqC8="
    }
    layer = {
      path          = "tests/fixtures/mock-artifact.txt"
      sha256_hex    = "19688284c4266ed2805347de8445d203fede7a35412ac593d2ab077c0316a82f"
      sha256_base64 = "GWiChMQmbtKAU0fehEXSA/7eejVBKsWT0qsHfAMWqC8="
    }
  }

  github_app_id         = "123456"
  github_private_key    = "mock-private-key"
  github_webhook_secret = "mock-webhook-secret-long-value"
  deepseek_api_key      = "mock-provider-key"
  openrouter_api_key    = "mock-openrouter-key"
}

run "default_provider_excludes_direct_environment" {
  command = plan
  variables {
    direct_openai = {
      api_key = "unused-synthetic-key"
      profile = "unused-profile"
      budget  = "unused-budget"
    }
  }
  assert {
    condition = (
      aws_lambda_function.pipeline.environment[0].variables["MODEL_PROVIDER"] == "openrouter" &&
      !contains(keys(aws_lambda_function.pipeline.environment[0].variables), "OPENAI_API_KEY") &&
      !contains(keys(aws_lambda_function.pipeline.environment[0].variables), "DIRECT_OPENAI_PROFILE") &&
      !contains(keys(aws_lambda_function.pipeline.environment[0].variables), "DIRECT_OPENAI_BUDGET")
    )
    error_message = "An inactive direct provider must not expose its credentials or configuration."
  }
  assert {
    condition = (
      aws_lambda_function.pipeline.environment[0].variables["DEEPSEEK_API_KEY"] == var.deepseek_api_key &&
      aws_lambda_function.pipeline.environment[0].variables["OPENROUTER_API_KEY"] == var.openrouter_api_key &&
      aws_lambda_function.pipeline.environment[0].variables["ANALYZER_MODEL"] == var.model_routing.analyzer_model &&
      aws_lambda_function.pipeline.environment[0].variables["DEEPSEEK_TRANSPORT_MODEL_OVERRIDE"] == var.model_routing.transport_model_override
    )
    error_message = "Existing provider environments retain both rollback credentials and legacy model overrides."
  }
}

run "direct_provider_requires_inputs" {
  command = plan
  variables {
    model_provider = "direct_openai"
  }
  expect_failures = [var.direct_openai]
}

run "direct_provider_rejects_invalid_json" {
  command = plan
  variables {
    model_provider = "direct_openai"
    direct_openai = {
      api_key = "synthetic-key"
      profile = "not-json"
      budget  = "{}"
    }
  }
  expect_failures = [var.direct_openai]
}

run "direct_provider_rejects_nonobject_budget" {
  command = plan
  variables {
    model_provider = "direct_openai"
    direct_openai = {
      api_key = "synthetic-key"
      profile = "{}"
      budget  = "[]"
    }
  }
  expect_failures = [var.direct_openai]
}

run "direct_provider_requires_nonempty_key" {
  command = plan
  variables {
    model_provider = "direct_openai"
    direct_openai = {
      api_key = " "
      profile = "{}"
      budget  = "{}"
    }
  }
  expect_failures = [var.direct_openai]
}

run "direct_provider_opt_in_preserves_inert_defaults" {
  command = plan
  variables {
    model_provider = "direct_openai"
    direct_openai = {
      api_key = "synthetic-key"
      profile = jsonencode({ version = "synthetic-unqualified" })
      budget  = jsonencode({ authorization = "unapproved" })
    }
  }
  assert {
    condition = (
      aws_lambda_function.pipeline.environment[0].variables["MODEL_PROVIDER"] == "direct_openai" &&
      aws_lambda_function.pipeline.environment[0].variables["OPENAI_API_KEY"] == var.direct_openai.api_key &&
      aws_lambda_function.pipeline.environment[0].variables["DIRECT_OPENAI_PROFILE"] == var.direct_openai.profile &&
      aws_lambda_function.pipeline.environment[0].variables["DIRECT_OPENAI_BUDGET"] == var.direct_openai.budget &&
      aws_lambda_function.pipeline.environment[0].variables["DRY_RUN"] == "true" &&
      aws_lambda_event_source_mapping.pipeline.enabled == false
    )
    error_message = "Direct configuration is explicit environment wiring, not traffic activation or runtime qualification."
  }
  assert {
    condition = alltrue([
      for name in [
        "DEEPSEEK_API_KEY", "OPENROUTER_API_KEY", "ANALYZER_MODEL", "ANALYZER_EFFORT",
        "DEEPSEEK_MODEL", "DEEPSEEK_REASONING_EFFORT", "DEEPSEEK_TRANSPORT_MODEL_OVERRIDE",
        "LOW_REVIEW_MODEL", "LOW_REVIEW_EFFORT", "NORMAL_REVIEW_MODEL", "NORMAL_REVIEW_EFFORT",
        "PFR_NORMAL_MODEL", "PFR_NORMAL_EFFORT"
      ] : !contains(keys(aws_lambda_function.pipeline.environment[0].variables), name)
    ])
    error_message = "Direct mode must omit unused provider secrets and legacy routing keys to preserve the environment budget."
  }
  assert {
    condition = (
      contains(keys(aws_lambda_function.pipeline.environment[0].variables), "DEEPSEEK_TRACE_MODE") &&
      contains(keys(aws_lambda_function.pipeline.environment[0].variables), "DEEPSEEK_TRACE_S3_BUCKET")
    )
    error_message = "The sole provider client still needs its compatible trace configuration."
  }
  assert {
    condition     = !contains(keys(aws_lambda_function.webhook.environment[0].variables), "OPENAI_API_KEY")
    error_message = "The Webhook must not receive provider credentials."
  }
}
