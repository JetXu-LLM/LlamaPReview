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

Its opening sentence never varies. The single invitation that follows is chosen
from the reviewed head SHA out of a fixed code-owned set, so a retry, a
recovery, and a rebuild of the same review always produce the same footer, while
different pull requests surface different entry points into the project.
Rendering strips every footer the code can emit before appending one, so a body
prepared under one head and rebuilt under another still ends with exactly one.

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

## Inline comments

Inline comments are placement requests, not independent reviews. Code validates path, side, changed-line eligibility, ranges, payload fields, size, and suggestion form. If a valid finding cannot be placed safely, it can move into a bounded fallback section in the main body without changing the finding's judgment.

Post-merge follow-ups create no inline comments. Existing inline requests are
folded, in their original order, into a `### Follow-up locations` main-body
section using exact `path:line — action` locations.

## Mermaid

Mermaid is optional. Eligibility and syntax are checked, unsafe content is sanitized, and an invalid diagram can be removed without suppressing an otherwise valid review. The system does not manufacture a diagram to satisfy a quota.

## Failure and skip messages

Terminal messages are code-owned and deliberately narrow. They do not claim a model judgment that was not completed. A provider error, unsafe projection, stale head, closed pull request, or policy skip cannot be transformed into a substantive “looks good” review.

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
