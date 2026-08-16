# Changelog

This file records user-visible, security-relevant, and operator-relevant changes
for each project release. Entries use semantic versions and group changes under
`Added`, `Changed`, `Fixed`, `Security`, and `Removed` when those headings add
real information.

GitHub release notes may provide more detail, but they do not replace this
maintained summary. Numeric AWS Lambda versions are deployment identities and
do not appear as project versions.

## [Unreleased]

### Added

- Typed lifecycle dispositions and durable initial-admission evidence across
  exact-head Pipeline checkpoints.
- One bounded early successor for an open pull request whose head changes
  before the first expensive PFR Reconcile.

### Changed

- The Pipeline stops remaining model work when an exact admitted head is
  merged or closed, publishes a deterministic cancellation when appropriate,
  and converts a completed same-head merged result into a post-merge follow-up.
- Post-merge follow-ups keep substantive findings and valid diagrams while
  folding inline requests into the main body and removing merge-gate posture.
- Publication candidates bind their exact lifecycle disposition as well as
  head and payload identity.

### Fixed

- Avoided expensive PFR or Final calls after a lifecycle checkpoint has already
  proved that the review can no longer continue on its admitted head.

## [0.1.1] - 2026-08-13

### Changed

- Representation-sensitive blocking findings now require exact postimage or
  equivalent source evidence instead of relying on unified-diff decoration.
- Exact-head CI evidence retains failed, pending, incomplete, and partial state
  ahead of green noise, while Deep remains responsible for causality and
  relevance.

### Fixed

- Prevented visible all-green or unconditional-safe language from contradicting
  structured red, pending, incomplete, or partial CI evidence.
- Removed empty Mermaid groups and exact duplicate review-check bullets through
  deterministic Projection while preserving valid diagrams and material
  findings.
- Added public Terraform topology verification and clarified that the public
  repository is the sole active runtime source.

## [0.1.0] - 2026-08-10

### Added

- The active public-only Webhook and exact-head review Pipeline used by the
  official hosted service.
- Deterministic Webhook, Pipeline, and dependency-Layer release artifacts with
  checksums, SBOM, dependency-license inventory, and GitHub provenance.
- Generic AWS self-hosting infrastructure, architecture and operator docs,
  replay/parity contracts, and contributor/community surfaces.

### Changed

- New hosted private-repository events are acknowledged and discarded before
  durable product state, provider processing, or GitHub product API calls.
- Substantive main review bodies carry one restrained, code-owned open-source
  footer; inline and nonpublishable messages do not.

### Removed

- Legacy Handler and Advanced Handler runtimes, rollout lists, shadow/canary
  routing, and private operational history from the public active product.

[Unreleased]: https://github.com/JetXu-LLM/LlamaPReview/compare/v0.1.1...HEAD
[0.1.1]: https://github.com/JetXu-LLM/LlamaPReview/releases/tag/v0.1.1
[0.1.0]: https://github.com/JetXu-LLM/LlamaPReview/releases/tag/v0.1.0
