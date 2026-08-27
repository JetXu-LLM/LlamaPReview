# Hosted and self-hosted modes

## Official hosted service

The official GitHub App reviews supported pull request events from public repositories. Its running Webhook, Pipeline, and dependency bytes are independently verified against one exact attested semantic release from this repository. Each deployment records that exact release identity; this guide deliberately does not hard-code a version that will become stale.

- Installation: [GitHub App](https://github.com/apps/llamapreview)
- Product site: [jetxu-llm.github.io/LlamaPReview-site](https://jetxu-llm.github.io/LlamaPReview-site/)
- Source and releases: [JetXu-LLM/LlamaPReview](https://github.com/JetXu-LLM/LlamaPReview)

Private-repository events are acknowledged and discarded at the signed Webhook boundary. They do not enter the review Pipeline. See [privacy and retention](PRIVACY.md).

## Self-hosted AWS deployment

The reference Terraform deploys the same two active Lambdas and one dependency Layer. It is intentionally public-repository-only as shipped; there is no hidden private-repository mode, legacy handler, shadow router, or repository allowlist. Its `pipeline_capacity_policy` defaults to the whole-policy literal `off`, so a self-hoster paying for its own provider account does not inherit the personally funded hosted-service bounds. That literal disables quota counters only and leaves one-time head succession enabled; only an explicit `successor=off` key disables succession. See [configuration](CONFIGURATION.md#free-review-capacity).

Self-hosters provide and pay for their own AWS, GitHub App, and DeepSeek accounts. They also become responsible for:

- GitHub App installation scope and permissions;
- AWS access, cost controls, alarms, log retention, and incident response;
- protecting Lambda environment configuration and Terraform state;
- selecting retention periods that meet their obligations;
- reviewing DeepSeek's current terms for their use case;
- verifying release artifacts before deployment.

Follow [AWS deployment](AWS_DEPLOYMENT.md) rather than copying official production names or state. The public repository contains no production account, role, bucket, backend, or rollback identity.

## Behavioral parity

Hosted and self-hosted deployments share the same admission, retrieval, judgment, projection, accounting, recovery, and publication code. Deployment configuration can change resource names, retention, budgets, and model routing; it should not silently change the review-output contract.

That shared contract includes lifecycle rereads before consequential work,
at most one early successor when enabled by the public configuration,
deterministic cancellation when an exact admitted head ends before review
completion, and an exact-head post-merge follow-up only when Final was already
publishable. These behaviors use the same native review publication and
recovery transaction; they do not require a placeholder comment, a second
publication surface, or wider GitHub App permissions.
