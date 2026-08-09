# Changelog

This file records user-visible, security-relevant, and operator-relevant changes
for each project release. Entries use semantic versions and group changes under
`Added`, `Changed`, `Fixed`, `Security`, and `Removed` when those headings add
real information.

GitHub release notes may provide more detail, but they do not replace this
maintained summary. Numeric AWS Lambda versions are deployment identities and
do not appear as project versions.

## [Unreleased]

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

[Unreleased]: https://github.com/JetXu-LLM/LlamaPReview/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/JetXu-LLM/LlamaPReview/releases/tag/v0.1.0
