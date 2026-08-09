# Troubleshooting

## Webhook returns 401

Confirm that GitHub and Lambda use the same webhook secret, that the raw request body reaches Lambda unchanged, and that the `X-Hub-Signature-256` header is preserved. Do not print either the secret or the full webhook payload while diagnosing it.

## Public pull request is acknowledged but no item appears

Only non-draft `opened` and `ready_for_review` pull request actions are admitted. Confirm that GitHub reports `repository.private` as `false`, the event contains an installation ID and head SHA, and the Webhook role can conditionally put into the active table.

Private events are expected to leave no item or identity-bearing application log.

## Item remains pending or context-ready

Check the existing DynamoDB stream mapping, Lambda concurrency, phase claim expiry, Pipeline error alarm, and Pipeline logs. Preserve the item and publication artifacts: deleting state can destroy the evidence needed for safe exactly-once recovery.

## Review is superseded

The pull request head changed, closed, or merged after admission. This is an expected fail-closed result. A later supported webhook event can admit the new exact head; do not force publication of the old payload.

## Inline comment moved into the main body

Placement degrades locally when the requested file/line/range is not a safe changed-line target. Inspect the diff mapping and suggestion shape. Do not weaken exact placement checks merely to preserve an inline surface.

## Mermaid is absent

Diagrams are optional and must be eligible, bounded, and syntactically safe. The review remains valid when Projection removes only an unsafe or unsupported diagram.

## Provider usage is incomplete

Treat an unresolved dispatch fence, missing token class, model-identity mismatch, or partition mismatch as an accounting failure. Do not estimate away a provider call or publish cost claims from a partial ledger.

## Terraform plan exposes secret values

Saved plans and state can contain sensitive Lambda environment values even when Terraform marks variables sensitive. Stop printing the plan, move all temporary files outside the repository with mode `0600`, restrict state access, and rotate only if an actual exposure is confirmed.

## Release verification fails

Do not deploy. Re-download into a clean temporary directory and verify the canonical repository, tag, commit, attestation, release manifest, filenames, and SHA-256 checksums. A locally rebuilt or renamed ZIP is a different artifact even if its source appears equivalent.
