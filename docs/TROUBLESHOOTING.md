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

An early open-head change may create one successor. In public configuration,
an empty `PIPELINE_CAPACITY_POLICY` and the literal `off` both keep that
succession enabled; `off` disables quota counters only. Check for an explicit
`successor=off` key if an early changed head stopped without requeueing. A late
head change always supersedes silently, regardless of that setting.

## Pull request shows “Review unavailable”

Paid review work completed, but Final or deterministic Projection still could
not produce a reliable review after normal retries. The message deliberately
makes no merge recommendation. Check the private failure stage, provider
ledger, exact-head disposition, and immutable publication receipt; do not
replace it with model prose or manually rerun publication. It is excluded from
ordinary review and quality metrics.

The notice is valid only for an open, explicitly unlocked pull request on the
same exact head and is checked again before persistence and dispatch. A new
head, ended or locked pull request, unknown lock state, pre-Final failure, or
unknown provider-dispatch outcome stays private or follows its existing typed
lifecycle outcome. The final fresh snapshot can produce
`publication_pre_dispatch_abort` after the intent says `dispatching` but before
`create_review`; `publication_post_started=false` is the proof of zero GitHub
writes. A `dispatching` intent without that typed terminal marker or a receipt
remains outcome-unknown, so recovery must reconcile it instead of posting
another notice.

## Inline comment moved into the main body

Placement degrades locally when the requested file/line/range or snippet is not
a unique safe changed-line target. The snippet is an anchor request, not the
finding's evidence. Inspect the admitted evidence references, diff mapping,
and suggestion shape separately. An evidenced finding may remain in the main
body after its snippet and inline surface are removed; a finding whose deciding
evidence failed must not be rescued by moving its prose.

## PFR reports a symbol miss for a runner or workflow

If the exact-head file was already fetched completely and is within the
full-file evidence cap, a missed optional symbol anchor now admits that bounded
full file instead of manufacturing an unknown. A large, truncated, absent,
binary, or policy-excluded file still requires its existing typed recovery or
remains unavailable; do not relabel partial content as full-file evidence.

## Review degraded after an evidence reference changed

Projection may remove an invalid supporting reference, one invalid required
reference when another admitted required reference for the same item survives,
or a nondeciding item whose evidence no longer validates. The decision and
first-screen copy must contract with the removed dependency. If the last
deciding basis is gone, core prose still depends on invalid evidence, or the
fixed schema/size bounds cannot be satisfied safely, the correct result is
nonpublication and—only when its separate lifecycle conditions are met—the
code-owned `Review unavailable` notice.

## Mermaid is absent

Diagrams are optional and must be eligible, bounded, and syntactically safe. The review remains valid when Projection removes only an unsafe or unsupported diagram.

## Provider usage is incomplete

First distinguish the typed dispatch state. A
`provider_dispatch_fence_unavailable` failure occurred before HTTP, is
retryable, and proves zero provider dispatch. A
`provider_dispatch_outcome_unknown` state means an earlier durable dispatch
fence has no terminal transport result; it is not retryable, a second call is
withheld, and numeric usage remains incomplete. Treat a missing token class,
model-identity mismatch, or partition mismatch as an accounting failure. Do not
infer these states from message text, estimate away a call, or publish cost
claims from a partial ledger.

## Terraform plan exposes secret values

Saved plans and state can contain sensitive Lambda environment values even when Terraform marks variables sensitive. Stop printing the plan, move all temporary files outside the repository with mode `0600`, restrict state access, and rotate only if an actual exposure is confirmed.

## Release verification fails

Do not deploy. Re-download into a clean temporary directory and verify the canonical repository, tag, commit, attestation, release manifest, filenames, and SHA-256 checksums. A locally rebuilt or renamed ZIP is a different artifact even if its source appears equivalent.
