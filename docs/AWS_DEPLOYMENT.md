# AWS self-hosting deployment

The reference Terraform stack deploys one public-repository review path: an alias-qualified Webhook Function URL, one DynamoDB lifecycle table and stream, an immutable Pipeline alias, one dependency Layer, a private S3 bucket, least-privilege IAM, logs, alarms, and one event-source mapping.

It contains no official account identity, production backend, legacy handler, rollout allowlist, automatic deploy role, paid secret service, or private-repository mode. AWS and DeepSeek usage incur costs.

## Prerequisites

- Terraform 1.10 or later, below 2.0;
- AWS credentials authorized to create only the resources in `infra/terraform`;
- an encrypted, versioned, public-blocked S3 backend that you control;
- a GitHub App ID, private key, and webhook secret;
- a DeepSeek API key;
- one fully verified LlamaPReview release.

Follow [release verification](RELEASE_VERIFICATION.md) before preparing Terraform inputs. Do not rebuild different function bytes during deployment.

## Protect secrets and state

The GitHub private key, webhook secret, and provider key are stored in Lambda environment configuration, encrypted at rest with AWS's built-in encryption and protected by IAM. They are also present in Terraform state. This design intentionally adds no Secrets Manager, Parameter Store SecureString, or customer-managed KMS key.

Treat these permissions and locations as secret access:

- `lambda:GetFunctionConfiguration`;
- the Terraform state bucket and lock state;
- local variable projections and saved plans;
- deployment-artifact storage and Lambda update permissions.

Keep backend configuration and secret-bearing variables outside the repository. Create them with mode `0600`, never print them, never pass secret values as command-line arguments, and delete temporary projections after the bounded operation.

## Initialize and plan

From `infra/terraform`, copy the example to an external protected directory and replace every placeholder with your own values and verified artifact hashes:

```bash
secure_dir="$(mktemp -d)"
chmod 700 "${secure_dir}"
cp terraform.tfvars.example "${secure_dir}/llamapreview.tfvars"
chmod 600 "${secure_dir}/llamapreview.tfvars"
```

Create a private backend configuration in the same directory. Then initialize, validate, and save the exact plan:

```bash
terraform init -reconfigure -backend-config="${secure_dir}/backend.hcl"
terraform fmt -check -recursive
terraform validate
terraform plan \
  -var-file="${secure_dir}/llamapreview.tfvars" \
  -out="${secure_dir}/llamapreview.tfplan"
terraform show "${secure_dir}/llamapreview.tfplan"
```

Reject a plan that contains resources beyond the documented two functions, one Layer, one table, one bucket, their active aliases/URL/ESM, least-privilege IAM, required logs, and alarms.

The first plan must retain the safe defaults:

```hcl
pipeline_stream_enabled = false
pipeline_dry_run        = true
pipeline_capacity_policy = "off"
```

These defaults are inert because the ESM is disabled. If it is later enabled
while `pipeline_dry_run=true`, the Pipeline suppresses GitHub publication but
still writes the durable AWS recovery, accounting, and artifact state described
in [configuration](CONFIGURATION.md#dry_run). Use the isolated local validation
path when the acceptance boundary is zero AWS product writes.

Terraform independently checks both hexadecimal and base64 SHA-256 identities before uploading each artifact.

## Apply and inspect the inert stack

Apply only the saved plan you inspected:

```bash
terraform apply "${secure_dir}/llamapreview.tfplan"
terraform output webhook_release
terraform output pipeline_release
terraform output private_storage
terraform output alarm_names
```

Before enabling traffic, verify:

- both `LIVE` aliases point to published numeric versions;
- the two function code hashes and Layer hash match the verified release;
- Webhook uses Python 3.12/x86_64, 180 seconds, and 128 MB;
- Pipeline uses Python 3.12/x86_64, 900 seconds, and the configured memory;
- the ESM is disabled and targets the Pipeline `LIVE` alias;
- the ESM filter admits only `PENDING` and `CONTEXT_READY` items, so capacity
  sentinels never invoke the Pipeline;
- `PIPELINE_CAPACITY_POLICY=off` unless this self-hoster deliberately chose its
  own bounded policy;
- DynamoDB TTL/PITR, S3 public blocks/encryption/versioning/lifecycle, log retention, alarms, and IAM match the plan;
- the secret environment values were preserved without appearing in output or logs.

Invoke each numeric version with an empty safe event and inspect only content-safe errors/import status. An empty invocation must not create a GitHub product write.

## Connect and enable deliberately

Set the GitHub App webhook endpoint to the alias-qualified `webhook_function_url` output and subscribe only to the pull-request event used by the handler. Do not widen App permissions beyond those required by the current code.

After safe validation, create and inspect a second saved plan that changes only the intended activation values. Enable the existing ESM deliberately. Set `pipeline_dry_run=false` only when this deployment is meant to publish reviews:

```hcl
pipeline_stream_enabled = true
pipeline_dry_run        = false
```

Apply the saved activation plan, then require a zero-drift plan. Watch alarms and logs while exercising one controlled public pull request at a frozen exact head.

## Rollback

Before every update, record the current Webhook and Pipeline numeric versions, alias revision IDs, Layer identity, ESM UUID/state, artifact hashes, and saved plan. A rollback moves aliases back to already verified immutable versions using compare-and-swap revision conditions; disable only the existing Pipeline ESM when drain safety requires it.

Do not delete tables, buckets, records, old Lambda versions, roles, logs, or traces as part of a release rollback. Investigate wrong-head or duplicate publication, unreconciled provider use, malformed payloads, secret/private identity exposure, private-event persistence, import/timeout/ESM failure, or artifact mismatch before reactivation.

See the narrower Terraform-root notes in [`infra/terraform/README.md`](../infra/terraform/README.md) and [troubleshooting](TROUBLESHOOTING.md).
