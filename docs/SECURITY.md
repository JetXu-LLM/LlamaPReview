# Security model

## Boundaries enforced by code

- webhook bodies are HMAC-SHA256 verified before parsing;
- private hosted events stop before identity construction, persistence, provider work, or GitHub product APIs;
- pull request lifecycle and head are reread at each consequential phase;
- repository tools exclude common sensitive paths and cap reads, searches, traces, and context;
- prompts and model output are untrusted inputs to typed validation and deterministic sanitation;
- inline placement and Mermaid are validated and may degrade locally;
- provider dispatches have durable fences and complete usage accounting;
- GitHub publication uses an immutable candidate, intent, digest, exact head, and reconciliation receipt;
- DRY_RUN blocks GitHub publication;
- operational logs and exception paths redact secrets and avoid untrusted exception text at the Webhook boundary.

## Secrets

The reference deployment uses Lambda environment variables protected by Lambda's built-in encryption at rest and least-privilege IAM. It intentionally adds no Secrets Manager, Parameter Store SecureString, customer-managed KMS key, sidecar, or paid secret service.

This design makes state and configuration access security-critical. Operators must restrict:

- `lambda:GetFunctionConfiguration`;
- Terraform state and saved plans;
- release and deployment buckets;
- Lambda configuration updates and alias movement;
- local files that project secret values into Terraform.

Public CI has no production AWS role, credentials, state access, artifact-bucket access, or deployment permission. Official production deployment is a separate operator action that consumes verified public release artifacts.

## Supply-chain boundary

Release automation builds only the active Webhook, Pipeline, and one dependency Layer. It double-builds deterministic artifacts, publishes hashes and an SBOM/license inventory, scans source/history/artifacts, and creates GitHub provenance. A self-hoster or official operator should reject an artifact whose repository, tag, commit, filename, manifest, checksum, or attestation does not agree.

## What the system does not guarantee

- LlamaPReview does not execute pull request code, but it does read public repository content and CI evidence.
- Sensitive-path filters cannot repair secrets already exposed in a public diff, comment, filename, or external provider surface.
- Model output can be incomplete or wrong; it is review assistance, not a security certification.
- AWS service encryption does not protect data from an IAM principal authorized to read it.
- A public GitHub review remains subject to GitHub and repository retention.

Report vulnerabilities through the private process in [`SECURITY.md`](../SECURITY.md).
