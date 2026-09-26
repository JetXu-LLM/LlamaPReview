# Configuration and model routing

The reference Terraform separates secret values from non-secret behavior configuration. Do not commit either real values or generated Terraform plans/state.

## Required secrets

| Variable | Purpose |
| --- | --- |
| `GITHUB_WEBHOOK_SECRET` | HMAC verification of webhook bodies |
| `GITHUB_APP_ID` | GitHub App authentication identity |
| `GITHUB_PRIVATE_KEY` | GitHub App JWT signing key |
| `OPENROUTER_API_KEY` | OpenRouter API authentication for the default Luna profile |
| `DEEPSEEK_API_KEY` | Direct DeepSeek API authentication retained for one-setting rollback |

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

`MODEL_PROVIDER=openrouter` selects `openai/gpt-6-luna` through OpenRouter
Chat Completions at `max` reasoning for every active model phase. The active
phases use JSON outputs and execute repository tools in application code; they
do not send provider function tools. The transport omits DeepSeek's `thinking`
field and sets OpenRouter `provider.require_parameters=true` so routing cannot
silently discard max reasoning or JSON format. It records the requested model,
returned model, provider, token classes,
cache details, and reported gateway cost in the existing fenced call ledger.
An absent gateway cost stays absent; token prices are not treated as a billed
receipt. OpenRouter's [model catalog](https://openrouter.ai/openai/gpt-6-luna)
and [Chat API](https://openrouter.ai/docs/api/api-reference/chat/send-chat-completion-request)
describe the exact identifier and request fields.

`MODEL_PROVIDER=deepseek` restores the prior DeepSeek endpoint, phase routing,
and reasoning profile in one setting when both provider keys are already
configured. The following controls apply only to that rollback profile:

- `ANALYZER_MODEL` / `ANALYZER_EFFORT`
- `LOW_REVIEW_MODEL` / `LOW_REVIEW_EFFORT`
- `PFR_NORMAL_MODEL` / `PFR_NORMAL_EFFORT`
- `NORMAL_REVIEW_MODEL` / `NORMAL_REVIEW_EFFORT`
- `DEEPSEEK_MODEL` / `DEEPSEEK_REASONING_EFFORT`

`DEEPSEEK_TRANSPORT_MODEL_OVERRIDE` changes the exact model identifier sent to DeepSeek without rewriting the logical routing tier. The provider ledger records both identities. Set it to the exact empty string only when direct logical-model dispatch has been deliberately validated.

`MODEL_PROVIDER` must be exactly `openrouter` or `deepseek`. A missing key for
the selected provider fails before any HTTP dispatch; the transport never
silently switches providers. Luna with function tools at `max` would require
the Responses API, so the Chat transport rejects such a request instead of
quietly lowering reasoning effort. This follows the [official OpenAI GPT-6
migration guidance](https://developers.openai.com/api/docs/guides/latest-model).

## Budgets and deadlines

Context size, tool rounds, provider timeouts, and phase deadlines are bounded by the variables in [`config.py`](../lambdas/LlamaPReviewPipeline/config.py). Treat those defaults as a coherent tested profile. A larger value can raise Lambda duration, provider cost, DynamoDB/S3 pressure, and the probability that a head changes before publication.

With `MODEL_PROVIDER=openrouter`, normal and high PFR both allow up to 600
seconds for retrieval steps and use a 780-second PFR time budget. The context
phase still has its separate 780-second limit and 30-second state-write
reserve; tool, token, and context-size caps remain different by review tier.
This longer retrieval window prevents a max-reasoning Plan that exceeds the
legacy 180-second gate from automatically skipping its planned checks. It is
not a guarantee that every Plan and Reconcile finishes within the phase limit.
`MODEL_PROVIDER=deepseek` restores the previous normal 180/240-second and high
420/780-second soft/time budgets. The four `PFR_*TIME_BUDGET_SECONDS` settings
can explicitly override these profile defaults.

On the deployed Lambda main thread, a POSIX wall timer bounds each provider
HTTP request and body read even if the peer sends bytes slowly enough to keep
resetting a socket read timeout. It preserves a shorter outer Review timer.
If an in-flight attempt expires, the existing dispatch ledger records unknown
usage and makes no automatic second call. Non-main-thread or non-POSIX local
callers retain only the socket timeout and phase checks.

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
| `repo_daily` | `3` | Admitted paid runs per repository per UTC day. It must be `0`–`512`; `0` removes only the repository bound while the global bound remains active. |
| `global_daily` | `100` | Circuit breaker across all repositories per UTC day. It must be `1`–`512` for an enabled policy. |
| `successor` | `on` | `off` disables one-time head succession for this public runtime configuration. Accepted on-values are `on`, `true`, `1`, and `yes`; accepted off-values are `off`, `false`, `0`, and `no`. |

Successor control is independent from quota enablement:

- an empty value keeps the source defaults: `repo_daily=3`,
  `global_daily=100`, and successor enabled;
- the whole-policy literal `off` disables both quota counters and still leaves
  successor enabled;
- only a compact key `successor=off` disables succession; omitted quota keys
  retain their source defaults;
- the public Terraform variable `pipeline_capacity_policy` defaults to the
  whole-policy literal `off`, so the self-hosted public default is unbounded
  capacity with one-time succession enabled.

For example, this explicitly disables succession while retaining the bounded
source quota values:

```text
repo_daily=3;global_daily=100;successor=off
```

These are public source and reference-Terraform semantics. They do not state
the effective successor value of any separately operated deployment; read that
deployment's immutable configuration when operational proof is required.

The code-owned maximum of `512` applies to both daily bounds. The global maximum
bounds the admission-ID set and the per-repository counter/notice attributes on
the single daily DynamoDB sentinel; the repository maximum also rejects huge
integers before boto3 can attempt DynamoDB decimal serialization. An enabled
policy with `global_daily=0`, either value above `512`, an unknown or duplicate
key, or an invalid value is rejected rather than silently creating unsafe state.
Use the literal `off` when both quotas should be disabled.

The Pipeline rereads the effective successor flag after claiming context work.
When an explicit `successor=off` is active, a successor that was already queued
stops silently before source retrieval, capacity admission, or new paid work.
Retained terminal predecessor-call ledgers remain on that item. A publication
intent or unresolved provider dispatch continues through its existing
fail-closed recovery path instead of being discarded by the operator switch.

Each UTC day uses one reserved sentinel in the existing table at
`pr_number = -1`, so it cannot collide with a pull request or with the repository
fact sheet at `pr_number = 0`. One atomic conditional update records the exact
run admission and charges both active counters. A retry of the same repository,
pull request, run ID, head, and successor disposition reuses that day's
admission rather than consuming capacity again within the same UTC day. A retry
after the UTC boundary is admitted against the new day's quota. Capacity sentinel stream records are
filtered before Lambda invocation and also rejected by the handler boundary.

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
