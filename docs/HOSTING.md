# Hosted and self-hosted modes

## Official hosted service

The official GitHub App reviews supported pull request events from public repositories. It runs the source and immutable release artifacts published from this repository.

- Installation: [GitHub App](https://github.com/apps/llamapreview)
- Product site: [jetxu-llm.github.io/LlamaPReview-site](https://jetxu-llm.github.io/LlamaPReview-site/)
- Source and releases: [JetXu-LLM/LlamaPReview](https://github.com/JetXu-LLM/LlamaPReview)

Private-repository events are acknowledged and discarded at the signed Webhook boundary. They do not enter the review Pipeline. See [privacy and retention](PRIVACY.md).

## Self-hosted AWS deployment

The reference Terraform deploys the same two active Lambdas and one dependency Layer. It is intentionally public-repository-only as shipped; there is no hidden private-repository mode, legacy handler, shadow router, or repository allowlist.

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
