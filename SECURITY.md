# Security Policy

## Report a vulnerability privately

Do not open a public issue for a suspected vulnerability. Use
[GitHub private vulnerability reporting](https://github.com/JetXu-LLM/LlamaPReview/security/advisories/new).

Do not include real credentials, private repository content, raw provider
payloads, installation IDs, private logs, or production state. Prefer a
synthetic or redacted reproduction.

When possible, include:

- the affected release, tag, or commit;
- the affected capability and deployment mode;
- a concise impact statement;
- minimal reproduction steps using public or synthetic data;
- any known mitigation.

## Security-sensitive boundaries

Reports are especially useful when they concern:

- webhook signature verification or public/private admission;
- unintended repository reads or sensitive-path handling;
- secret exposure in configuration, logs, traces, artifacts, or errors;
- wrong-head, duplicate, or unauthorized GitHub publication;
- provider transport or accounting integrity;
- release-artifact provenance, dependency integrity, or AWS permissions.

## Supported versions

Only the latest GitHub release is expected to receive security fixes. The
default branch may contain unreleased work and is not a supported production
release.

The project does not currently offer a bug bounty or a guaranteed response or
remediation time. Please allow maintainers to investigate before public
disclosure.
