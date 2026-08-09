locals {
  webhook_function_name  = "${var.name_prefix}-webhook"
  pipeline_function_name = "${var.name_prefix}-pipeline"
  layer_name             = "${var.name_prefix}-dependencies"
  table_name             = "${var.name_prefix}-pipeline-runs"
  runtime_prefix         = "pipeline"
  trace_prefix           = "${local.runtime_prefix}/deepseek-traces/"

  tags = merge(
    {
      ManagedBy = "Terraform"
      Project   = "LlamaPReview"
    },
    var.tags,
  )
}
