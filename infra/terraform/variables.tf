variable "aws_region" {
  description = "AWS Region for the complete self-hosted stack."
  type        = string
  default     = "us-east-1"

  validation {
    condition     = can(regex("^[a-z]{2}(-[a-z]+)+-[0-9]+$", var.aws_region))
    error_message = "aws_region must be a valid AWS Region name."
  }
}

variable "name_prefix" {
  description = "Lowercase prefix for resources created by this stack."
  type        = string
  default     = "llamapreview"

  validation {
    condition = (
      length(var.name_prefix) >= 3 &&
      length(var.name_prefix) <= 32 &&
      can(regex("^[a-z0-9][a-z0-9-]*[a-z0-9]$", var.name_prefix))
    )
    error_message = "name_prefix must be 3-32 lowercase letters, numbers, or interior hyphens."
  }
}

variable "artifact_bucket_name" {
  description = "Optional globally unique name for the private release/runtime artifact bucket. A generated name is used when null."
  type        = string
  default     = null
  nullable    = true

  validation {
    condition = (
      var.artifact_bucket_name == null ||
      can(regex("^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$", var.artifact_bucket_name))
    )
    error_message = "artifact_bucket_name must be null or a valid lowercase S3 bucket name."
  }
}

variable "release_artifacts" {
  description = "Verified local release artifacts and their exact SHA-256 identities. Terraform uploads only these bytes."
  type = object({
    webhook = object({
      path          = string
      sha256_hex    = string
      sha256_base64 = string
    })
    pipeline = object({
      path          = string
      sha256_hex    = string
      sha256_base64 = string
    })
    layer = object({
      path          = string
      sha256_hex    = string
      sha256_base64 = string
    })
  })

  validation {
    condition = alltrue([
      for artifact in values(var.release_artifacts) :
      can(regex("^[0-9a-fA-F]{64}$", artifact.sha256_hex)) &&
      can(regex("^[A-Za-z0-9+/]{43}=$", artifact.sha256_base64))
    ])
    error_message = "Every artifact needs a 64-character hexadecimal and 44-character base64 SHA-256."
  }
}

variable "github_app_id" {
  description = "GitHub App numeric ID. This is an identifier, not a secret."
  type        = string

  validation {
    condition     = can(regex("^[0-9]+$", var.github_app_id))
    error_message = "github_app_id must contain only digits."
  }
}

variable "github_private_key" {
  description = "GitHub App private key. Stored in encrypted Lambda configuration and Terraform state."
  type        = string
  sensitive   = true

  validation {
    condition     = length(trimspace(var.github_private_key)) > 0
    error_message = "github_private_key must not be empty."
  }
}

variable "github_webhook_secret" {
  description = "Shared HMAC secret used to verify every GitHub webhook before admission."
  type        = string
  sensitive   = true

  validation {
    condition     = length(var.github_webhook_secret) >= 16
    error_message = "github_webhook_secret must be at least 16 characters."
  }
}

variable "deepseek_api_key" {
  description = "DeepSeek API key used by the Pipeline. Stored in encrypted Lambda configuration and Terraform state."
  type        = string
  sensitive   = true

  validation {
    condition     = length(trimspace(var.deepseek_api_key)) > 0
    error_message = "deepseek_api_key must not be empty."
  }
}

variable "model_routing" {
  description = "Current logical and transport model routing. Change only with matching behavioral validation."
  type = object({
    deep_model               = string
    transport_model_override = string
    deep_effort              = string
    analyzer_model           = string
    analyzer_effort          = string
    low_review_model         = string
    low_review_effort        = string
    normal_pfr_model         = string
    normal_pfr_effort        = string
    normal_review_model      = string
    normal_review_effort     = string
  })
  default = {
    deep_model               = "deepseek-v4-pro"
    transport_model_override = "deepseek-v4-flash"
    deep_effort              = "max"
    analyzer_model           = "deepseek-v4-flash"
    analyzer_effort          = "high"
    low_review_model         = "deepseek-v4-flash"
    low_review_effort        = "high"
    normal_pfr_model         = "deepseek-v4-flash"
    normal_pfr_effort        = "high"
    normal_review_model      = "deepseek-v4-pro"
    normal_review_effort     = "high"
  }

  validation {
    condition = alltrue([
      for value in values(var.model_routing) : length(trimspace(value)) > 0
    ])
    error_message = "Every model-routing field must be non-empty."
  }
}

variable "provider_trace_mode" {
  description = "Provider trace retention: off, content-free summary logs, or full private S3 traces."
  type        = string
  default     = "summary"

  validation {
    condition     = contains(["off", "summary", "full"], var.provider_trace_mode)
    error_message = "provider_trace_mode must be off, summary, or full."
  }
}

variable "pipeline_dry_run" {
  description = "When true, the Pipeline performs no GitHub product writes. Keep true for initial validation."
  type        = bool
  default     = true
}

variable "pipeline_stream_enabled" {
  description = "Whether the DynamoDB Stream invokes the immutable Pipeline LIVE alias. Enable only after safe smoke validation."
  type        = bool
  default     = false
}

variable "pipeline_memory_size" {
  description = "Pipeline Lambda memory in MB."
  type        = number
  default     = 600

  validation {
    condition     = var.pipeline_memory_size >= 512 && var.pipeline_memory_size <= 10240
    error_message = "pipeline_memory_size must be between 512 and 10240 MB."
  }
}

variable "pipeline_ttl_days" {
  description = "Days before DynamoDB pipeline state becomes eligible for TTL deletion."
  type        = number
  default     = 30

  validation {
    condition     = var.pipeline_ttl_days >= 1 && floor(var.pipeline_ttl_days) == var.pipeline_ttl_days
    error_message = "pipeline_ttl_days must be a positive whole number."
  }
}

variable "artifact_retention_days" {
  description = "Days to retain runtime context, review, accounting, and publication artifacts."
  type        = number
  default     = 30

  validation {
    condition     = var.artifact_retention_days >= 1 && floor(var.artifact_retention_days) == var.artifact_retention_days
    error_message = "artifact_retention_days must be a positive whole number."
  }
}

variable "full_trace_retention_days" {
  description = "Days to retain full private provider traces when provider_trace_mode is full."
  type        = number
  default     = 7

  validation {
    condition = (
      var.full_trace_retention_days >= 1 &&
      var.full_trace_retention_days <= var.artifact_retention_days &&
      floor(var.full_trace_retention_days) == var.full_trace_retention_days
    )
    error_message = "full_trace_retention_days must be a positive whole number no greater than artifact_retention_days."
  }
}

variable "webhook_log_retention_days" {
  description = "CloudWatch retention for content-safe Webhook logs."
  type        = number
  default     = 30
}

variable "pipeline_log_retention_days" {
  description = "CloudWatch retention for Pipeline logs and content-free provider summaries."
  type        = number
  default     = 90
}

variable "dynamodb_point_in_time_recovery" {
  description = "Enable DynamoDB point-in-time recovery for durable pipeline state."
  type        = bool
  default     = true
}

variable "dynamodb_deletion_protection" {
  description = "Protect the pipeline state table from accidental deletion. Disable deliberately before destroy."
  type        = bool
  default     = true
}

variable "alarm_actions" {
  description = "Optional SNS topic ARNs for alarm notifications. Empty still creates visible alarms."
  type        = list(string)
  default     = []
}

variable "tags" {
  description = "Additional tags applied to every taggable resource."
  type        = map(string)
  default     = {}
}
