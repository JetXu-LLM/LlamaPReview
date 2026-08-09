# AWS self-hosting reference

This Terraform root deploys one production path:

```text
signed GitHub Function URL -> immutable Webhook LIVE alias
  -> one DynamoDB table and stream
  -> immutable Pipeline LIVE alias + one dependency Layer
  -> exact-head review publication
```

It creates no legacy handlers, rollout allowlists, automatic GitHub deployment
role, dashboard, paid secret service, or official LlamaPReview account identity.
The supplied Function, Pipeline, and Layer ZIPs are external release inputs;
Terraform rejects them unless their bytes match both declared SHA-256 forms.

## Before planning

Use Terraform 1.10 or later and configure an encrypted, versioned, private S3
backend at `terraform init` time. Backend coordinates intentionally do not live
in this repository. Lambda environment variables and Terraform state contain
your GitHub private key, webhook secret, and provider key, so access to state
and `lambda:GetFunctionConfiguration` is secret access.

`backend.hcl.example` shows the required backend settings without containing
real coordinates. Create and secure that bucket separately, enable versioning
and public-access blocking, and restrict both object and lock-file access to
your Terraform operators.

Download the three assets and checksum manifest from one trusted LlamaPReview
release. Verify provenance and the manifest before using the artifact paths.
Copy `terraform.tfvars.example` to a mode-0600 file outside the repository,
replace every placeholder, and never pass secret values as shell arguments or
commit them.

Typical initialization uses your own backend configuration:

```bash
terraform init -reconfigure -backend-config=/secure/path/backend.hcl
terraform fmt -check -recursive
terraform validate
terraform plan -out=/secure/path/llamapreview.tfplan \
  -var-file=/secure/path/llamapreview.tfvars
terraform show /secure/path/llamapreview.tfplan
terraform apply /secure/path/llamapreview.tfplan
```

The first apply should retain the defaults `pipeline_stream_enabled=false` and
`pipeline_dry_run=true`. Inspect the numeric versions, aliases, artifact hashes,
Function URL policy, table, bucket, alarms, and empty-event Lambda imports.
Then enable the same ESM deliberately. Set `pipeline_dry_run=false` only when
you intend the Pipeline to publish GitHub reviews.

`pipeline_dry_run=true` blocks GitHub publication; it does not block DynamoDB,
S3, logs, or provider calls once stream processing is enabled. Keep the ESM
disabled for an inert deployment. Use the isolated local validation procedure,
not this AWS flag, when the required boundary is zero AWS product writes.

The Webhook URL is public because GitHub cannot use AWS IAM authentication.
The handler verifies the GitHub HMAC signature before admission, and private
repository events stop before durable product state. Current AWS Function URL
behavior adds the two URL-only public invoke permissions required for
`authorization_type = "NONE"`; do not broaden them into general invocation.

## Storage, privacy, and cost

- Lambda encrypts environment configuration at rest with its AWS-managed key;
  this reference creates no customer-managed KMS key, Secrets Manager secret,
  or Parameter Store value.
- The one S3 bucket blocks public access, enforces bucket ownership and TLS,
  uses SSE-S3 (`AES256`), and versions release/runtime objects.
- Content-addressed release artifacts have no expiry. Runtime artifacts expire
  after 30 days by default; full private provider traces expire after 7 days.
  `provider_trace_mode="summary"` is the default and stores no full S3 trace.
- DynamoDB uses on-demand billing, AWS-owned encryption, TTL, and point-in-time
  recovery by default. TTL deletion is asynchronous.
- Webhook logs default to 30 days and Pipeline logs to 90 days.
- Lambda, DynamoDB, S3, CloudWatch Logs, alarms, provider calls, data transfer,
  and optional SNS notifications can incur charges.

The artifact bucket refuses Terraform deletion while it contains objects, and
the table has deletion protection by default. Disable protection only as a
deliberate teardown step. Historical records are never migrated or deleted by
this stack.

## Development validation

No AWS credentials are needed for syntax or mocked topology checks:

```bash
terraform init -backend=false
terraform validate
terraform test
```

Mock tests never invoke Lambda or create cloud resources. A real plan still
requires your chosen AWS credentials, private backend, release artifacts, and
secret-bearing tfvars.
