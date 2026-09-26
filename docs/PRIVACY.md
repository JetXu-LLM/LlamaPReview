# Privacy and data retention

This document distinguishes source defaults from the official hosted deployment. The retention observations below were recorded on **August 12, 2026**. The source now defaults to OpenRouter; this change alone does not establish that an operator has deployed the new provider configuration.

## Hosted repository boundary

The official service supports public repositories only.

After verifying the GitHub webhook signature, the Webhook reads the event kind and repository visibility. If GitHub marks the repository private, the application returns a generic success acknowledgement before it constructs durable identity or enters the Pipeline. It performs no product DynamoDB/S3 write, model provider call, or GitHub product API call, and writes no repository name, pull request number, delivery ID, installation ID, account identity, head SHA, title, or payload to application logs.

This boundary applies to new events. Historical private-repository records from earlier service behavior were not deleted, migrated, inventoried, backfilled, or exposed during the open-source launch. They remain in private operator-controlled storage under their existing lifecycle and access controls.

## What a public review processes

For an admitted public pull request, LlamaPReview processes:

- repository, pull request, installation, branch, and exact-head identifiers;
- changed files and diffs;
- bounded related public repository content retrieved for evidence;
- pull request lifecycle and selected CI evidence;
- model prompts, responses, usage, and accounting metadata;
- the prepared and published GitHub review payload.

General sensitive-path rules prevent the retrieval tools from reading common secrets, private keys, credentials, `.env` files, and similar paths. That is a defense in depth, not permission to commit secrets to a public repository.

## Observed official retention

| Surface | Current hosted behavior |
| --- | --- |
| DynamoDB lifecycle records | `ttl_epoch` is set to 30 days; DynamoDB TTL deletion is asynchronous |
| S3 context, review, and recovery artifacts | lifecycle expiry after 30 days |
| S3 provider trace objects | lifecycle expiry after 7 days |
| Webhook CloudWatch logs | 30 days |
| Pipeline CloudWatch logs | 90 days |
| GitHub review comments | retained by GitHub and the repository until removed there |

The hosted S3 bucket is private, uses S3-managed encryption at rest, blocks public access, and uses bucket-owner-enforced object ownership. Versioning is currently disabled. The active DynamoDB table uses encryption at rest, point-in-time recovery, and TTL.

Default provider traces are summaries: identities, model routing, usage, timing, and tool counts, without prompts or model output. Recovery artifacts can contain public repository code, model-derived review content, and GitHub payload material.

## Model provider processing

The default `MODEL_PROVIDER=openrouter` sends bounded public-repository evidence to OpenRouter for `openai/gpt-6-luna`. OpenRouter routes the request to a serving provider. Gateway logging, account settings, and the serving provider's policies are separate from LlamaPReview's AWS retention. This repository does not promise categorical zero retention or no training for a deployment; consult [OpenRouter's privacy policy](https://openrouter.ai/privacy) and [provider routing and data-policy controls](https://openrouter.ai/docs/guides/routing/provider-selection).

`MODEL_PROVIDER=deepseek` sends the same bounded evidence directly to DeepSeek. It is an explicit operator switch, with no automatic cross-provider fallback.

DeepSeek's current Open Platform terms govern API use, while its current privacy policy says that processing rules for end-user personal data in downstream applications are the developer's responsibility. Those documents do not give this service a categorical no-training or fixed-retention promise for API inputs. DeepSeek's API documentation also says disk context caching is enabled by default and unused cache entries are usually cleared within a few hours to a few days.

- [DeepSeek Open Platform Terms](https://cdn.deepseek.com/policies/en-US/deepseek-open-platform-terms-of-service.html)
- [DeepSeek Privacy Policy](https://cdn.deepseek.com/policies/en-US/deepseek-privacy-policy.html)
- [DeepSeek context caching documentation](https://api-docs.deepseek.com/guides/kv_cache/)

Do not submit private source or secrets to the official hosted service. Self-hosters should review the provider's current terms and select a deployment model appropriate to their obligations.

## AWS and GitHub

AWS runs the Webhook, Pipeline, storage, and logs. GitHub supplies webhook data, repository evidence, installation authentication, and the final review surface. Data in transit uses HTTPS/TLS; retained AWS data uses service encryption at rest. See [security](SECURITY.md) for the limits of those controls.

## Requests and questions

For privacy questions about the official service, use the contact route on the [product site](https://jetxu-llm.github.io/LlamaPReview-site/privacy.html). Security vulnerabilities should follow [`SECURITY.md`](../SECURITY.md), not a public issue.
