# Changelog

This file records user-visible, security-relevant, and operator-relevant changes
for each project release. Entries use semantic versions and group changes under
`Added`, `Changed`, `Fixed`, `Security`, and `Removed` when those headings add
real information.

GitHub release notes may provide more detail, but they do not replace this
maintained summary. Numeric AWS Lambda versions are deployment identities and
do not appear as project versions.

## [Unreleased]

## [0.1.6] - 2026-08-27

### Changed

- Deterministic Projection now documents one strict local-degradation rule:
  evidence truth and public placement are separate contracts. Invalid optional
  supporting references, an invalid snippet/anchor, or a nondeciding item can
  be removed locally while admitted evidence and the surviving decision remain
  intact. A partially invalid required-reference list is retained only when at
  least one admitted required reference still supports that item. Projection
  fails closed when the last deciding basis is lost, truth-dependent core prose
  remains tainted, or a fixed schema or size bound cannot be satisfied without
  invention.
- The first screen leads with the primary retained merge-deciding finding or
  merge-deciding unknown and its immediate owner action. Every additional
  finding or unknown remains in details; the first screen reports only the
  count of further items instead of repeating their prose.
- PFR spends the existing bounded budget on concrete author acceptance
  criteria first; authoritative validation wiring when the pull request changes
  tests, CI, or validation infrastructure second; Route's highest-consequence
  locally answerable fact third; and general exploration only with remaining
  capacity. Equivalent string-form acceptance criteria are normalized into the
  internal criterion shape instead of being discarded. When a small exact-head
  file was already fetched in full but optional symbol anchors miss, the full
  bounded file remains usable evidence rather than becoming a false unknown.
  When repository inventory exposes both a validation runner and its separate
  discovery or configuration candidate, PFR plans both bounded reads up front
  instead of treating the thin wrapper as complete or relying only on a later
  Reconcile follow-up. When author acceptance material also claims the changed
  validation executes in CI, the same bounded plan follows the chain through
  the workflow or CI invocation that supplies its command and environment;
  missing exact workflow paths are located with one bounded search or directory
  listing, displacing lower-value exploration without increasing any budget.
  The reserved removed-symbol check remains the sole deterministic ordering
  exception.
- Public successor configuration is explicit: an empty
  `PIPELINE_CAPACITY_POLICY` and the literal policy `off` both keep one-time
  head succession enabled; the literal `off` disables only quota counters.
  Only an explicit `successor=off` key disables succession. The public
  Terraform default remains `off`, so self-hosted quota bounds are disabled
  while succession remains enabled.

### Fixed

- GitHub publication now distinguishes a typed
  `publication_pre_dispatch_abort`, which records
  `publication_post_started=false` after a fresh lifecycle mismatch prevents
  `create_review`, from `publication_outcome_unknown`, where the POST may have
  started and exact reconciliation is required. A durable intent may already
  say `dispatching`; only the typed abort proves zero GitHub writes.
- The code-owned `Review unavailable` notice now has an explicit lifecycle:
  it is eligible only after completed paid work and exhausted Final/Projection
  retries on the same open, unlocked head; it is revalidated and published
  through the ordinary exact-head transaction. New-head, ended, locked,
  quota/successor, pre-Final, and unknown-dispatch outcomes remain silent or
  keep their existing typed terminal policy.

## [0.1.5] - 2026-08-24

### Added

- A short, code-owned `Review unavailable` native review when paid model work
  completed but bounded retries still could not produce a reliable Final or
  Projection. It is exact-head, idempotent, score-excluded, footer-free, and
  suppressed for stale, ended, locked, quota, successor, and uncertain
  provider-dispatch outcomes.
### Changed

- Deep now opens with the pull-request objective, objective closure, and merge
  posture. Final must preserve those commitments, so an ordinary nondeciding
  unknown cannot silently rewrite the verdict. Multi-surface repair claims are
  closed only when exact evidence covers every decisive surface; an unchanged
  path cannot be credited as repaired merely because it remains coherent.
  Objective closure supplements raw-delta discovery rather than replacing an
  independently evidenced compile, startup, runtime, security, or data-integrity
  failure.
- PFR spends its existing question, round, and token budgets on author-stated
  acceptance criteria first, then the highest-consequence locally answerable
  Route fact, and only then general exploration. Ordinary plan order is
  preserved across tool types.
- A verified `test-gap` may carry a blocking decision only when the overall
  verdict is blocking and it names a concrete pre-merge owner action.

### Fixed

- Conditional clear reviews retain their model-owned code rationale before a
  separate exact-head CI-pending paragraph. Pending, missing, or unrelated CI
  no longer replaces that rationale or mechanically changes the verdict.
- A priority PFR follow-up can now broaden an already-read file's bounded
  symbol slice under the existing one-read soft-budget rescue. A prior slice
  no longer makes the whole path look complete, and cached source is reused
  without another repository fetch. The rescue remains bound to the earliest
  matching Plan read even if Reconcile serializes lower-priority follow-ups
  first.
- Exact symbol reads now recognize exported asynchronous TypeScript functions
  and exported constants as their own definitions even when the installed SDK's
  diff-context hints do not. A requested wrapper can no longer be silently
  attributed to an earlier helper and returned without its own body.
- Removed-symbol extraction now recognizes public and scoped-public Rust
  declarations. Deleting a `pub struct`, `pub(crate) type`, or equivalent
  declaration therefore keeps its reserved exact usage search, so surviving
  impls and constructors reach judgment as compile-risk evidence.
- Removed the category-level contradiction that made an otherwise valid,
  evidence-backed blocking `test-gap` fail deterministic Projection.

## [0.1.4] - 2026-08-22

### Added

- A bounded daily review capacity per repository, with a global circuit
  breaker, charged after the deterministic skip gates and before the first paid
  call. `PIPELINE_CAPACITY_POLICY` retunes or disables both bounds, and a
  self-hosted deployment that funds its own provider account turns them off by
  default. Exact run admission is atomic and retry-idempotent on one daily
  sentinel, so a global rejection cannot consume repository capacity and one
  run cannot be charged twice within the same UTC day. Code-owned maxima of 512
  repository and global admissions reject unsafe numeric configurations, while
  the global bound keeps that sentinel below DynamoDB's item limit. Unsafe
  explicit policies fail closed.

### Changed

- The code-owned open-source footer keeps a fixed opening sentence and now
  offers one invitation chosen from the reviewed head, so different pull
  requests surface different entry points while retry and recovery stay stable.
- Timeout values are unchanged in this release. New budget-invariant tests prove
  that the existing phase, provider, and invocation deadlines still compose
  inside the deployed Lambda timeout.

### Fixed

- Mermaid validation no longer accepts ordinary prose as a sequence message.
  An unanchored arrow pattern let backtracking split words such as `extract`
  into a participant, an arrow, and a second participant, so diagrams GitHub
  cannot render were published instead of degrading locally.
- Sequence diagrams with more than two `Note over` participants or an unquoted,
  case-insensitive reserved participant ID `Actor` now degrade locally instead
  of publishing syntax Mermaid 11.17.0 cannot parse. Quoted `"Actor"` IDs remain
  valid. Notes accept only `over`, `left of`, or `right of` placement; bare
  `left`, `right`, and `of` forms are rejected.
- A single-line `Note` followed by a bare continuation line is folded into the
  note instead of failing the whole diagram.
- `successor=off` now controls the actual one-time head-succession boundary,
  and capacity sentinel records cannot dispatch Pipeline work.

## [0.1.3] - 2026-08-17

### Fixed

- A merged or closed pull request whose native review surface is structurally
  locked now supersedes silently with complete accounting instead of turning
  GitHub's expected 422 rejection into a terminal publication error.

## [0.1.2] - 2026-08-17

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

[Unreleased]: https://github.com/JetXu-LLM/LlamaPReview/compare/v0.1.6...HEAD
[0.1.6]: https://github.com/JetXu-LLM/LlamaPReview/compare/v0.1.5...v0.1.6
[0.1.5]: https://github.com/JetXu-LLM/LlamaPReview/compare/v0.1.4...v0.1.5
[0.1.4]: https://github.com/JetXu-LLM/LlamaPReview/compare/v0.1.3...v0.1.4
[0.1.3]: https://github.com/JetXu-LLM/LlamaPReview/compare/v0.1.2...v0.1.3
[0.1.2]: https://github.com/JetXu-LLM/LlamaPReview/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/JetXu-LLM/LlamaPReview/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/JetXu-LLM/LlamaPReview/releases/tag/v0.1.0
