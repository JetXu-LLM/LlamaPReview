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

## Inline comments

Inline comments are placement requests, not independent reviews. Code validates path, side, changed-line eligibility, ranges, payload fields, size, and suggestion form. If a valid finding cannot be placed safely, it can move into a bounded fallback section in the main body without changing the finding's judgment.

## Mermaid

Mermaid is optional. Eligibility and syntax are checked, unsafe content is sanitized, and an invalid diagram can be removed without suppressing an otherwise valid review. The system does not manufacture a diagram to satisfy a quota.

## Failure and skip messages

Terminal messages are code-owned and deliberately narrow. They do not claim a model judgment that was not completed. A provider error, unsafe projection, stale head, closed pull request, or policy skip cannot be transformed into a substantive “looks good” review.

## Stable publication identity

Publication identity binds the exact head SHA and canonical request digests for the main body and inline list. The intentional open-source footer changes the public Markdown and therefore the digest, but retries of that prepared release reuse the same digest and do not append another footer.
