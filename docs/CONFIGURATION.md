# Configuration and model routing

The reference Terraform separates secret values from non-secret behavior configuration. Do not commit either real values or generated Terraform plans/state.

## Required secrets

| Variable | Purpose |
| --- | --- |
| `GITHUB_WEBHOOK_SECRET` | HMAC verification of webhook bodies |
| `GITHUB_APP_ID` | GitHub App authentication identity |
| `GITHUB_PRIVATE_KEY` | GitHub App JWT signing key |
| `DEEPSEEK_API_KEY` | Direct DeepSeek API authentication |

AWS Lambda encrypts environment variables at rest. That does not make function configuration public-safe: IAM principals that can read function configuration, Terraform state, saved plans, or deployment inputs may be able to recover secret values. Restrict those surfaces and keep temporary secret projections outside the repository with mode `0600`.

## Required resource configuration

| Variable | Purpose |
| --- | --- |
| `DYNAMODB_PIPELINE_TABLE` | Durable lifecycle, accounting, and publication state |
| `RUN_ARTIFACT_BUCKET` | Context, review, and recovery artifacts |
| `PUBLICATION_ARTIFACT_BUCKET` | Immutable publication candidates; normally the run-artifact bucket |
| `DEEPSEEK_TRACE_S3_BUCKET` | Bounded provider traces; normally the run-artifact bucket |

## Review routing

Route selects `skip`, `low`, `normal`, or `high` from the pull request and bounded repository facts. Low and normal tiers use smaller evidence and review budgets. High uses the full bounded PFR path. The model still owns the engineering decision inside the admitted evidence; repository-specific keyword rules do not replace that judgment.

The principal model controls are:

- `ANALYZER_MODEL` / `ANALYZER_EFFORT`
- `LOW_REVIEW_MODEL` / `LOW_REVIEW_EFFORT`
- `PFR_NORMAL_MODEL` / `PFR_NORMAL_EFFORT`
- `NORMAL_REVIEW_MODEL` / `NORMAL_REVIEW_EFFORT`
- `DEEPSEEK_MODEL` / `DEEPSEEK_REASONING_EFFORT`

`DEEPSEEK_TRANSPORT_MODEL_OVERRIDE` changes the exact model identifier sent to DeepSeek without rewriting the logical routing tier. The provider ledger records both identities. Set it to the exact empty string only when direct logical-model dispatch has been deliberately validated.

## Budgets and deadlines

Context size, tool rounds, provider timeouts, and phase deadlines are bounded by the variables in [`config.py`](../lambdas/LlamaPReviewPipeline/config.py). Treat those defaults as a coherent tested profile. A larger value can raise Lambda duration, provider cost, DynamoDB/S3 pressure, and the probability that a head changes before publication.

## Free review capacity

The hosted service is free for public repositories and funded personally, so a
daily bound keeps one high-velocity repository from consuming the shared budget.
Capacity is charged after the deterministic skip gates and before Route, so an
over-capacity pull request costs no model call, and traffic that would have been
skipped for free never consumes capacity.

`PIPELINE_CAPACITY_POLICY` is a single compact `key=value;key=value` string
rather than one variable per bound, because Lambda's 4KB environment budget is
already nearly consumed:

| Key | Default | Meaning |
| --- | --- | --- |
| `repo_daily` | `3` | Admitted paid runs per repository per UTC day. `0` removes the bound. |
| `global_daily` | `100` | Circuit breaker across all repositories per UTC day. `0` removes the bound. |
| `successor` | `on` | `off` disables one-time head succession without a redeploy. |

An empty value keeps these defaults; the literal `off` disables both bounds.
Self-hosted deployments that fund their own provider account will usually set
`PIPELINE_CAPACITY_POLICY=off`.

Counters live on reserved sentinel items in the existing table at
`pr_number = -1`, so they cannot collide with a pull request or with the
repository fact sheet at `pr_number = 0`.

## Tracing

`DEEPSEEK_TRACE_MODE` accepts:

- `summary` (default): model/usage/timing metadata without prompts or model output;
- `off`: no provider trace artifact;
- `full`: redacted request and response content.

Full traces materially increase retained source and model-output content. Enable them only with an explicit operational need and a suitably short bucket lifecycle.

## DRY_RUN

Global `DRY_RUN=true` or an explicit item-level `dry_run=true` executes the review path while blocking GitHub publication. Public tests prove that retired rollout strings cannot activate or bypass this barrier.

Inside the deployed Pipeline, DRY_RUN is a **GitHub write barrier**, not a stateless AWS sandbox. It may call the configured provider and it deliberately retains DynamoDB/S3 accounting, recovery state, and review artifacts. That durability is required to reconcile provider use and prove what happened after a retry or timeout.

Zero-product-write qualification is a separate, isolated operator procedure: run the exact public source against local fakes and local artifact destinations, with no production AWS resource configured. Do not send qualification traffic through the hosted Webhook or production stream and describe it as zero-write.
