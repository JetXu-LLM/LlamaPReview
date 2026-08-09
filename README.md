<div align="center">

# LlamaPReview

**Pull request review that reads the exact code you are about to merge.**

An evidence-first reviewer—not a chatbot wearing a review costume.

[![Visit LlamaPReview](https://img.shields.io/badge/Visit-LlamaPReview-20293a?style=for-the-badge)](https://jetxu-llm.github.io/LlamaPReview-site/)

[Install the GitHub App](https://github.com/apps/llamapreview) · [View source](https://github.com/JetXu-LLM/LlamaPReview)

[![CI](https://img.shields.io/github/actions/workflow/status/JetXu-LLM/LlamaPReview/ci.yml?branch=main&label=CI)](https://github.com/JetXu-LLM/LlamaPReview/actions/workflows/ci.yml)
[![License: Apache-2.0](https://img.shields.io/github/license/JetXu-LLM/LlamaPReview)](LICENSE)
[![Python 3.12](https://img.shields.io/badge/Python-3.12-3776AB)](https://www.python.org/)
[![Latest release](https://img.shields.io/github/v/release/JetXu-LLM/LlamaPReview)](https://github.com/JetXu-LLM/LlamaPReview/releases)

</div>

![LlamaPReview architecture: signed public-only admission, exact-head evidence and judgment, deterministic projection, durable recovery and accounting, and exactly-once GitHub publication](docs/assets/architecture.svg)

The Webhook and Pipeline source in this repository is the source used by the official hosted service. There is no separate reviewer hidden behind it.

LlamaPReview is an Apache-2.0 GitHub pull-request reviewer. Its hosted GitHub App reviews **public repositories only**. It differs from a generic review bot in where it draws the line between model judgment and code-owned guarantees:

- It retrieves **bounded evidence from the exact pull-request head**, with provenance and honest coverage gaps.
- The model performs **engineering judgment**: causal risk, severity, uncertainty, and merge posture.
- Deterministic code owns **projection, sanitation, inline placement, Mermaid safety, accounting, recovery, and publication identity**.

## How a review is built

1. **Verify the signed webhook.** No event is admitted before its GitHub signature is valid.
2. **Apply the hosted public-only boundary.** A private event is generically acknowledged after minimum visibility parsing, then stops before durable product state, provider work, or GitHub product API calls.
3. **Pin the exact head.** Admission and every later reread use the pull request's immutable head commit.
4. **Retrieve bounded evidence.** Route and PFR gather repository facts, provenance, coverage, and explicit gaps.
5. **Judge, then present.** Deep finds material engineering issues; Final compresses them into owner actions, inline requests, and an optional diagram.
6. **Project deterministically.** Code validates the public schema, caps content, sanitizes Mermaid, places comments, and degrades an invalid optional surface locally.
7. **Publish once to the same head.** Durable candidates, intents, receipts, and reconciliation make retries reuse the same prepared GitHub request.

## What gets published

A successful review contains one substantive main body, zero or more safely placed inline comments, and—when it materially clarifies the change—one eligible, sanitized Mermaid diagram. An unplaceable inline request can degrade into a bounded section in the main body without changing the underlying finding. Empty, skipped, failed, stale-head, and otherwise nonpublishable outcomes never acquire a synthetic model judgment.

## Hosted quick start

1. [Install the GitHub App](https://github.com/apps/llamapreview) on a **public** repository.
2. Open a pull request, or move an existing draft pull request to ready for review.
3. Read the resulting review as decision support; maintainers remain responsible for what they merge.

The hosted service is free for public repositories. Start at the [official website](https://jetxu-llm.github.io/LlamaPReview-site/) for the current product entry point.

## Self-hosted quick start

Self-hosting runs the same public-only Webhook and Pipeline path in your AWS account.

1. Download one semantic release and [verify its checksums and GitHub provenance](docs/RELEASE_VERIFICATION.md).
2. Follow the [AWS deployment guide](docs/AWS_DEPLOYMENT.md) to deploy the two Lambda functions, dependency Layer, DynamoDB table, private S3 bucket, event-source mapping, alarms, and least-privilege IAM.
3. Supply your own GitHub App and DeepSeek credentials, then pay your own AWS and provider costs.

The reference stack has no automatic production deployment, paid secret-management service, hidden private-repository mode, or official AWS identity.

## Privacy and security

For eligible public pull requests, selected public GitHub evidence, prompts, and generated output are sent to DeepSeek for engineering judgment. Official AWS product infrastructure runs in Singapore; model requests leave AWS for DeepSeek processing. DeepSeek documents default on-disk API context caching normally cleared within hours to days once unused, but its public Open Platform terms do not provide a categorical no-training assurance or one fixed overall API retention period.

Current official-service retention is explicit:

| Data | Retention |
|---|---:|
| DynamoDB public-run records | 30-day TTL; deletion is asynchronous |
| S3 public-run artifacts | 30 days |
| Full provider trace objects | 7 days |
| Webhook logs | 30 days |
| Pipeline logs | 90 days |

New private-repository events are discarded at the early hosted boundary. Historical private records were left untouched. See [privacy and retention](docs/PRIVACY.md) and the [security model](docs/SECURITY.md) for the full boundaries.

Runtime secret values stay in Lambda environment configuration, protected by AWS encryption at rest and strict IAM. Terraform state is secret-bearing and must remain encrypted, versioned, and private. Public CI has no official production credentials or deployment permission.

## Repository map

| Path | Responsibility |
|---|---|
| `lambdas/LlamaPReviewWebhookHandler` | Signed, public-only admission adapter |
| `lambdas/LlamaPReviewPipeline` | Retrieval, judgment, projection, persistence, accounting, and publication |
| `infra/terraform` | Generic AWS reference deployment |
| `scripts` | Deterministic builds, release verification, and safety checks |
| `tests` | Unit, replay, adversarial, recovery, and parity contracts |
| `docs` | Operator and contributor documentation |

Start with the [documentation index](docs/README.md) or read the [architecture](docs/ARCHITECTURE.md) in more depth.

## Development

Ordinary tests and replay fixtures make no paid provider calls.

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-ci.txt
make verify
```

Focused commands are available when iterating:

```bash
make test
make replay
```

See [development and testing](docs/DEVELOPMENT.md) for the exact local and release gates.

## Contributing

Contributors, subscribers, maintainers, and curious reviewers are all welcome. Use [Issues](https://github.com/JetXu-LLM/LlamaPReview/issues) for reproducible bugs and concrete work; use [Discussions](https://github.com/JetXu-LLM/LlamaPReview/discussions) for questions, ideas, and public review examples. Read [CONTRIBUTING.md](CONTRIBUTING.md) before opening a pull request, and report vulnerabilities through [SECURITY.md](SECURITY.md).

## License

LlamaPReview is licensed under the [Apache License 2.0](LICENSE). Dependency notices are recorded in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) and in each release inventory.
