resource "aws_s3_bucket" "artifacts" {
  bucket        = var.artifact_bucket_name
  bucket_prefix = var.artifact_bucket_name == null ? "${var.name_prefix}-artifacts-" : null
  force_destroy = false
}

resource "aws_s3_bucket_ownership_controls" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_public_access_block" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_versioning" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id

  rule {
    id     = "expire-full-provider-traces"
    status = "Enabled"

    filter {
      prefix = local.trace_prefix
    }

    expiration {
      days = var.full_trace_retention_days
    }

    noncurrent_version_expiration {
      noncurrent_days = var.full_trace_retention_days
    }
  }

  rule {
    id     = "expire-runtime-artifacts"
    status = "Enabled"

    filter {
      prefix = "${local.runtime_prefix}/"
    }

    expiration {
      days = var.artifact_retention_days
    }

    noncurrent_version_expiration {
      noncurrent_days = var.artifact_retention_days
    }
  }

  depends_on = [aws_s3_bucket_versioning.artifacts]
}

data "aws_iam_policy_document" "artifact_transport" {
  statement {
    sid    = "DenyInsecureTransport"
    effect = "Deny"

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    actions = ["s3:*"]
    resources = [
      aws_s3_bucket.artifacts.arn,
      "${aws_s3_bucket.artifacts.arn}/*",
    ]

    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_s3_bucket_policy" "artifact_transport" {
  bucket = aws_s3_bucket.artifacts.id
  policy = data.aws_iam_policy_document.artifact_transport.json

  depends_on = [aws_s3_bucket_public_access_block.artifacts]
}

resource "aws_s3_object" "release" {
  for_each = var.release_artifacts

  bucket                 = aws_s3_bucket.artifacts.id
  key                    = "release-artifacts/${each.key}/${lower(each.value.sha256_hex)}.zip"
  source                 = each.value.path
  source_hash            = lower(each.value.sha256_hex)
  content_type           = "application/zip"
  server_side_encryption = "AES256"

  lifecycle {
    precondition {
      condition     = filesha256(each.value.path) == lower(each.value.sha256_hex)
      error_message = "${each.key} artifact bytes do not match sha256_hex."
    }

    precondition {
      condition     = filebase64sha256(each.value.path) == each.value.sha256_base64
      error_message = "${each.key} artifact bytes do not match sha256_base64."
    }
  }

  depends_on = [
    aws_s3_bucket_ownership_controls.artifacts,
    aws_s3_bucket_server_side_encryption_configuration.artifacts,
    aws_s3_bucket_versioning.artifacts,
  ]
}

resource "aws_dynamodb_table" "pipeline" {
  name                        = local.table_name
  billing_mode                = "PAY_PER_REQUEST"
  hash_key                    = "repo"
  range_key                   = "pr_number"
  stream_enabled              = true
  stream_view_type            = "NEW_AND_OLD_IMAGES"
  deletion_protection_enabled = var.dynamodb_deletion_protection

  attribute {
    name = "repo"
    type = "S"
  }

  attribute {
    name = "pr_number"
    type = "N"
  }

  ttl {
    attribute_name = "ttl_epoch"
    enabled        = true
  }

  point_in_time_recovery {
    enabled = var.dynamodb_point_in_time_recovery
  }

  server_side_encryption {
    enabled = true
  }
}
