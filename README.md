<div align="center">

# LlamaPReview

**Open-source pull request review that reads the exact code you are about to merge.**

The hosted GitHub App is free for public repositories — and this repository is the code it runs.

[**Install the GitHub App**](https://github.com/apps/llamapreview) · [**Read the source**](https://github.com/JetXu-LLM/LlamaPReview/tree/main/lambdas) · [**Website**](https://jetxu-llm.github.io/LlamaPReview-site/) · [**Self-host it**](docs/HOSTING.md)

[![CI](https://img.shields.io/github/actions/workflow/status/JetXu-LLM/LlamaPReview/ci.yml?branch=main&label=CI)](https://github.com/JetXu-LLM/LlamaPReview/actions/workflows/ci.yml)
[![License: Apache-2.0](https://img.shields.io/github/license/JetXu-LLM/LlamaPReview)](LICENSE)
[![Python 3.12](https://img.shields.io/badge/Python-3.12-3776AB)](https://www.python.org/)
[![Latest release](https://img.shields.io/github/v/release/JetXu-LLM/LlamaPReview)](https://github.com/JetXu-LLM/LlamaPReview/releases)

</div>

There is no separate reviewer hidden behind the hosted service. The Webhook and Pipeline in this repository **are** the production source, released under Apache-2.0, and every review the App publishes comes from code on this page.

![How LlamaPReview turns a public pull request into a published review: a signed public GitHub event, exact-head admission and bounded evidence, DeepSeek engineering judgment, deterministic projection and publication, and a public review from source you can run](docs/assets/architecture.svg)

*The model supplies engineering judgment. Everything around it — evidence boundaries, output schema, sanitation, placement, and publication identity — is deterministic code you can read.*

## What a review actually looks like

Reviews are evidence-led and say so when evidence is missing. From a [real published review](https://github.com/Texarkanine/SumMem/pull/10#pullrequestreview-4978442337):

> ### LlamaPReview — Blocking issues found
>
> Do not merge until the nap instruction names the runnable driver path, matching the activation scheme this PR ships; currently the printed instruction tells agents to run `summem`, not `.summem/summem`.
>
> Exact-head CI remains unresolved (1 pending); no CI-dependent merge-safety claim is made.

Note the second paragraph. When the evidence does not support a claim, the review declines to make it rather than guessing. Here is [another public review](https://github.com/mmayasaurus/heddle/pull/66#pullrequestreview-4977897488), and the full [output contract](docs/REVIEW_OUTPUT.md) states exactly what will and will not be published.

## Why it differs from a generic review bot

- Evidence is retrieved from the **exact pull-request head**, with provenance and honest coverage gaps — not from a stale branch snapshot.
- The model performs **engineering judgment only**: causal risk, severity, uncertainty, and merge posture.
- Deterministic code owns **projection, sanitation, inline placement, Mermaid safety, accounting, recovery, and publication identity**, so an unsafe or unrenderable surface degrades locally instead of reaching your pull request.
- Empty, skipped, failed, and stale-head outcomes **never** acquire a synthetic "looks good" verdict.

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

The hosted service is free for public repositories and is funded personally, so each repository gets a small daily review capacity and a shared circuit breaker protects the rest. The first pull request that runs past the daily bound says so and stops before spending anything; later ones that day stop quietly. Self-hosting removes the bound entirely. Start at the [official website](https://jetxu-llm.github.io/LlamaPReview-site/) for the current product entry point.

## Self-hosted quick start

Self-hosting runs the same public-only Webhook and Pipeline path in your AWS account.

1. Download one semantic release and [verify its checksums and GitHub provenance](docs/RELEASE_VERIFICATION.md).
2. Follow the [AWS deployment guide](docs/AWS_DEPLOYMENT.md) to deploy the two Lambda functions, dependency Layer, DynamoDB table, private S3 bucket, event-source mapping, alarms, and least-privilege IAM.
3. Supply your own GitHub App and DeepSeek credentials, then pay your own AWS and provider costs.

The reference stack has no automatic production deployment, paid secret-management service, hidden private-repository mode, or official AWS identity.

Repository evidence is retrieved through [llama-github](https://github.com/JetXu-LLM/llama-github), an independently released SDK from the same author that you can use on its own.

## Privacy and security

For eligible public pull requests, selected public GitHub evidence, prompts, and generated output are sent to DeepSeek for engineering judgment. New private-repository events are discarded at the early hosted boundary, before any durable state, provider call, or GitHub product API call.

Official-service retention is explicit and bounded: public-run records and artifacts expire in 30 days, full provider traces in 7. The exact table, the DeepSeek terms this depends on, and the credential boundaries are documented in [privacy and retention](docs/PRIVACY.md) and the [security model](docs/SECURITY.md).

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
