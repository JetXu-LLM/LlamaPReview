# Contributing to LlamaPReview

Users, subscribers, first-time contributors, and experienced maintainers are all
welcome to improve LlamaPReview.

Use Issues for reproducible defects and focused, actionable changes. Use
Discussions for usage questions, open-ended ideas, and public review examples.
Report vulnerabilities through the private process in [SECURITY.md](SECURITY.md).

## Before you change code

- Read the README and the documentation for the capability you are changing.
- Keep the production path understandable: webhook admission, exact-head
  retrieval, engineering judgment, final presentation, deterministic
  projection, and exactly-once publication.
- Keep changes narrow. A new mode, provider, policy layer, dashboard, plugin
  system, or compatibility path needs a current product requirement.
- Search existing issues and pull requests before starting substantial work.

## Development and validation

The unit and replay suites are designed to run without provider credentials or
paid model calls. Useful local checks include:

```bash
python -m pytest tests/unit
python -m compileall -q lambdas scripts tests
```

Run targeted tests while developing, then run the repository's complete
documented gate before requesting review.

Paid provider tests require explicit maintainer intent. Never add a live API
call to the ordinary test path, and never use production credentials merely to
make a test pass. Any approved live validation must use a frozen public or
redacted input and the documented dry-run barriers.

## Change contract

- Add or update tests for behavior changes.
- Update current public documentation when behavior, configuration, output, or
  data handling changes.
- Keep review prompts and judgment general across repositories and languages.
- Do not replace model-owned engineering judgment with repository-specific
  keyword rules.
- Preserve exact-head checks, deterministic output safety, accounting truth,
  idempotency, and recovery guarantees unless the change explicitly and safely
  revises their contract.
- Keep generated release archives, local environments, caches, private traces,
  Terraform state, and credentials out of commits.

## Public evidence only

Use public, synthetic, or carefully redacted fixtures. Do not paste or commit:

- private repository source or diffs;
- credentials, tokens, private keys, or environment values;
- raw provider request or response payloads;
- installation IDs, private account identities, or private delivery metadata;
- private logs, traces, state snapshots, or production artifacts.

If useful evidence cannot be made public safely, describe the failure class and
ask a maintainer for a private route.

## Pull requests

Explain the user-visible outcome, why the owning capability is the right place
for the change, and exactly what you tested. Call out privacy, security,
accounting, publication, dependency, and documentation effects where relevant.

Public pull requests in this repository are reviewed by LlamaPReview itself.
That review is evidence for maintainers, not a replacement for human ownership
or the required checks.

Unless explicitly stated otherwise, contributions submitted for inclusion are
licensed under Apache-2.0. New dependencies must have a compatible license and
must update `THIRD_PARTY_NOTICES.md` and release inventory when applicable.
