# Review-output contract

LlamaPReview separates engineering judgment from public presentation.

## Main review

A substantive review has one model-derived body projected into deterministic Markdown. It may contain:

- merge posture and decision summary;
- prioritized findings with severity and material uncertainty;
- owner actions and verification guidance;
- a Mermaid diagram when architecture or flow materially benefits from one;
- fallback details for a suggestion that could not be safely anchored;
- one code-owned open-source footer.

The footer appears only in a trustworthy substantive main review body. It is never model-generated, never appended to individual inline comments, and is not added to empty, skipped, failed, or other nonpublishable terminal messages.

Its opening sentence never varies and states that the reviewer is open source,
because that is the one thing a reader cannot infer from the review itself. The
single invitation that follows is chosen from the reviewed head SHA out of a
fixed code-owned set, so a retry, a recovery, and a rebuild of the same review
always produce the same footer, while different pull requests surface different
entry points into the project. Rendering strips every footer the code can emit
before appending one, so a body prepared under one head and rebuilt under
another still ends with exactly one. The footer carries no tracking parameters.

For a clear code judgment whose exact-head CI snapshot is still pending,
incomplete, or unavailable, the existing `Conditional code-review clear`
title remains. The first paragraph preserves the model's code-level reason for
clear; a separate second paragraph states the unresolved CI fact. Code never
turns pending or missing CI into `verification_needed`. Deep changes the
posture only when that fact decides whether the pull-request objective is
actually achieved.

A `test-gap` is not categorically nonblocking. It may carry a blocking verdict
only with admitted required evidence and a concrete owner action that must be
completed before merge. A supporting-only test observation remains
nonblocking, and `question` and `note` never carry the merge decision.

An ordinary review is published only while the pull request is open on the
reviewed exact head. If a publishable Final finishes after that same head was
merged, deterministic Projection turns it into a post-merge follow-up:

```markdown
### LlamaPReview — Post-merge follow-up

This review started before the pull request was merged and completed afterward. It covers the exact merged PR head below; treat the findings as follow-up work, not a merge gate.
```

The follow-up preserves substantive findings, evidence, material uncertainty,
owner actions, CI facts, and any valid Mermaid diagram. Projection removes the
open-PR opening and merge-gate posture rather than pretending that the pull
request is still open. It retains exactly one code-owned footer.

## First-screen decision

The first screen is a bounded decision surface, not a second free-form review.
For a blocking review it opens from the primary retained merge-deciding
finding; for `verification_needed` it opens from the primary merge-deciding
unknown. The corresponding immediate owner action stays with that proof unit.
No secondary finding or unknown contributes prose or another action above the
fold. The first screen reports only how many further items remain, while their
full findings, checks, actions, and uncertainty stay in the details section.

If local Projection removes a proposed deciding item, it also removes that
item's dependent summary and action language. The first screen is then
contracted to the next retained deciding item. It never preserves unsupported
clauses from the original summary merely because they were well written. If no
admitted deciding basis remains, the review is nonpublishable rather than
rendered with a synthetic decision.

## Projection degradation

Projection treats evidence truth and public placement as separate contracts:

- Required evidence references support the finding's causal conclusion.
  Invalid supporting references may be removed locally. An unknown required
  reference may be removed only when another admitted required reference for
  that same item survives; losing the last required basis for a deciding item
  fails closed.
- A `code_snippet` is a placement anchor, not substitute evidence. If the
  snippet cannot be matched to one exact changed region, Projection removes the
  snippet and inline request while retaining an otherwise evidenced finding in
  the main body. An exact-representation claim and direct-replacement suggestion
  tied to that invalid snippet are removed too; the finding can survive as
  semantic prose only when its remaining exact-head evidence capability still
  supports it.
- A malformed or unsupported nondeciding item, optional confidence check,
  inline request, or Mermaid diagram may be removed without changing the
  surviving judgment. Any dependent clear or blocking first-screen prose is
  contracted at the same time.
- Projection does not guess paths, rewrite anchors, promote supporting
  evidence, or truncate through fixed truth-bearing schema and size bounds. An
  incomplete provider envelope, an unresolvable deciding dependency, tainted
  core prose, or an output that cannot satisfy those bounds makes the review
  nonpublishable.

## Inline comments

Inline comments are placement requests, not independent reviews and not proof
of a finding. Code validates path, side, changed-line eligibility, ranges,
payload fields, size, and suggestion form after evidence admission. If an
evidenced finding cannot be placed safely, it moves into a bounded fallback
section in the main body without changing the finding's judgment. If its
evidence contract fails, moving the prose is not a recovery mechanism.

Post-merge follow-ups create no inline comments. Existing inline requests are
folded, in their original order, into a `### Follow-up locations` main-body
section using exact `path:line — action` locations.

## Mermaid

Mermaid is optional. Eligibility and syntax are checked, unsafe content is sanitized, and an invalid diagram can be removed without suppressing an otherwise valid review. The system does not manufacture a diagram to satisfy a quota.

## Failure and skip messages

Terminal messages are code-owned and deliberately narrow. They do not claim a model judgment that was not completed. A provider error, unsafe projection, stale head, closed pull request, or policy skip cannot be transformed into a substantive “looks good” review.

When paid model work has completed, normal retries are exhausted, and Final or
Projection still cannot produce a reliable review, an open, unlocked pull
request on the same exact head may receive one native `COMMENT` review:

```markdown
### LlamaPReview — Review unavailable

LlamaPReview couldn't complete a reliable review of this pull request at this commit. No recommendation was made about whether it should be merged.
```

This message uses the ordinary exactly-once publication transaction but is not
a normal review or quality sample. It has no footer, inline comment, Mermaid,
error code, model identity, usage detail, owner action, or merge judgment, and
it consumes no additional capacity. A new head, ended or locked pull request,
quota/successor outcome, or unknown provider-dispatch outcome keeps its existing
silent or dedicated terminal policy.

Eligibility is checked only after the failed private artifact and complete
provider accounting exist. The exact head and typed open/unlocked disposition
are checked again before the immutable candidate is stored and before GitHub
dispatch. Immediately before `create_review`, a fresh pull-request snapshot is
checked again. A confirmed mismatch raises the typed
`publication_pre_dispatch_abort` and records
`publication_post_started=false`, proving zero GitHub writes even though the
durable intent may already have transitioned to `dispatching`. A `dispatching`
intent without that typed terminal proof or a receipt remains outcome-unknown
and must be reconciled. DRY_RUN prepares the same candidate but keeps the GitHub
write barrier in force.

A provider-call fence failure before HTTP is separately typed as
`provider_dispatch_fence_unavailable`: it is retryable and proves that provider
dispatch did not start. A prior durable `dispatching` fence without a terminal
transport outcome is `provider_dispatch_outcome_unknown`: the second call is
withheld, accounting remains incomplete, and no `Review unavailable` notice is
published. These states are never inferred from error-message text.

A pull request already merged or closed when initial admission runs is silent.
If the exact admitted head is merged or closed while review work is in
progress, before a publishable Final exists, the Pipeline may publish one
native `COMMENT` review and stop the remaining model work:

```markdown
### LlamaPReview — Review stopped

This pull request was merged before the review finished, so LlamaPReview stopped the remaining model work. No code-review verdict was produced.
```

A repository that has used its free daily capacity receives one skip notice for
the first affected pull request in that UTC day. Later pull requests in the same
day stop silently rather than repeating it, and a head successor never publishes
a capacity notice because that pull request already had its review.

The closed form changes only `merged` to `closed`. A cancellation contains no
footer, inline comment, Mermaid diagram, finding, verdict, or model-authored
text. It is not quality-scoreable and records the corresponding lifecycle
exclusion reason. If the head also changed, the old run is superseded silently
instead of publishing a stale cancellation.

GitHub may report the pull request review surface as locked. If that typed
state is observed before dispatch, a cancellation or post-merge follow-up is
not publishable and the run is superseded silently with
`publication_unavailable_locked`. No template, model text, inline comment,
footer, issue-comment fallback, or alternate credential is used. An unknown
lock state does not get guessed from arbitrary API error text.

## Stable publication identity

Publication identity binds the exact head SHA, required lifecycle disposition,
publication kind, and canonical request digests for the main body and inline
list. Ordinary reviews require open/same-head, cancellations require their
exact ended disposition, and post-merge follow-ups require merged/same-head.
The intentional open-source footer changes substantive public Markdown and
therefore the digest, but retries reuse the same prepared request and never add
a second footer or publication surface.
