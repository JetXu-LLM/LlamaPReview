# Development and testing

Python 3.11 and 3.12 are supported for development. Release Lambdas and the dependency Layer target Python 3.12 on Linux x86_64.

## First success

```bash
make setup
make test
```

Ordinary unit and replay tests use local fakes, synthetic inputs, or redacted fixtures. They make no paid model call and no GitHub/AWS product write.

## Useful targets

```bash
make lint        # focused formatting and static checks
make test        # unit tests plus replay corpus
make build       # three deterministic release ZIPs
make verify      # public-boundary, artifact, docs, and supply-chain checks
make terraform   # format and validate generic reference infrastructure
```

The exact target definitions live in the root [`Makefile`](../Makefile). CI is the authoritative clean-environment invocation.

## Behavioral changes

A review-behavior change should include:

1. a focused unit or adversarial test for the invariant;
2. a representative replay when the change touches Route, retrieval, Deep, Final, Projection, placement, accounting, or recovery;
3. current documentation if the public contract or operator behavior changes;
4. explicit evidence that unrelated prompt, routing, budget, and payload behavior did not drift.

Do not replace model-owned engineering judgment with repository-specific keywords. Prompts and behavior must remain general across repositories and languages.

Objective-closure or PFR-priority changes must prove that both continuation and
standalone planning receive the same fixed Route commitment. The tested order
is author acceptance criteria; authoritative validation wiring when tests, CI,
or validation infrastructure change; Route's highest-consequence locally
answerable fact; then general exploration. Ordinary steps retain that semantic
order across tool types, with only the reserved removed-symbol check ahead of
it. No question, round, token, model, or provider-call cap changes. Evaluation
may attribute evidence flow from the existing ledger and full private trace,
but production code must not parse model prose to decide whether the objective
closed.

PFR contract tests must tolerate only representation-equivalent acceptance
criteria: the canonical `{criterion: ...}` object and a nonempty string that
normalizes to that object. Retrieval tests must also prove that a missed symbol
can expose an already complete small exact-head file, while large or truncated
files still fail closed without a real bounded symbol hit.

Projection changes require adversarial cases that distinguish evidence truth
from placement. Cover mixed valid/invalid required references, removable
supporting references, invalid and deleted-region snippets, unplaceable inline
requests, nondeciding item removal, and fixed schema/size bounds. Prove that
each bad fragment is removed locally when a complete deciding basis survives,
that dependent first-screen prose is contracted to the primary retained
deciding item, that the first screen contains no secondary item prose/action,
and that loss of the last deciding basis or tainted core prose remains
nonpublishable. When an invalid snippet carried an exact representation or
direct replacement, prove those optional surfaces are removed before any
semantic-only finding survives. Do not accept guessed paths, anchor rewriting,
evidence promotion, or silent truncation as recovery.

Failure-notice changes require retry and crash-recovery coverage plus the full
open/new-head/merged/closed/locked matrix. Tests must prove one exact body-only
`COMMENT`, zero footer/inline/Mermaid, complete accounting, no extra capacity
charge, and no publication for a pre-Final failure or an unknown
provider-dispatch outcome. Cover the failed notice's pre-persist lifecycle
reread, DRY_RUN barrier, immutable candidate/intent recovery, and quality-score
exclusion.

Provider-dispatch control changes must prove the typed boundary on both sides
of HTTP: a pre-dispatch fence failure is retryable, makes zero HTTP calls, and
records that dispatch did not start; an existing unresolved `dispatching`
record is terminal, makes no second HTTP call, retains incomplete accounting,
and cannot emit a public failure notice.

GitHub publication tests must separately prove the last fresh lifecycle check.
A confirmed mismatch after the intent becomes `dispatching` but before
`create_review` raises `publication_pre_dispatch_abort`, makes zero GitHub POSTs,
and stores `publication_post_started=false`. A `dispatching` intent without that
typed terminal proof must remain outcome-unknown and enter exact
reconciliation; intent state alone is not proof that a write did or did not
start.

Lifecycle changes additionally require deterministic coverage for the full
state matrix: same/new head crossed with open/merged/closed, plus an unverified
snapshot. Tests must prove durable initial admission, one bounded early
successor, silent late supersession, cancellation and post-merge rendering,
disposition-bound dispatch and recovery, complete predecessor accounting, and
unchanged ordinary open-PR output. Callback-order tests must place lifecycle
checks before the first Reconcile and before Final without changing the normal
number of bounded PFR rounds.

Successor-configuration tests must keep public source semantics explicit: the
empty policy and literal `off` both leave one-time succession enabled; only an
explicit `successor=off` disables it. A queued successor disabled before work
starts must stop before retrieval, capacity admission, and provider dispatch,
while an existing publication intent or unresolved provider dispatch keeps its
fail-closed recovery owner.

Lifecycle publication tests must also cover a structurally locked ended PR.
Cancellation and post-merge follow-up must make zero GitHub calls, preserve
complete accounting, and persist `publication_unavailable_locked`; unlocked
and unknown-lock fixtures retain the normal publication contract. Recovery
may terminalize a prepared mismatch or a typed
`publication_pre_dispatch_abort` only when the boundary records
`publication_post_started=false`. A `dispatching` intent without that typed
zero-write proof still uses duplicate/outcome reconciliation because its write
may have crossed the external boundary.

Replay fixtures should exercise lifecycle transitions with synthetic public
facts and make both cancellation and post-merge Markdown available for manual
inspection. A release candidate must also pass Python 3.11 and 3.12 unit and
replay tests, focused lint/static checks, compile/import checks,
`git diff --check`, deterministic double builds, Linux x86_64/Python 3.12 Layer
imports, and the source/history/artifact, dependency, license, SBOM, checksum,
manifest, and provenance gates in `make verify`.

GitHub platform compatibility may be checked with a deliberately selected
public, already-merged fixture and the normal native `COMMENT` review transport.
Such a probe must use no provider, make no AWS product write, bind an exact
commit, add no inline comments, and prove the before/after GitHub surface. A
permission, authentication, identity, or API-contract failure is a release stop
condition; it must not fall back to an issue comment or another credential.

After deployment, acceptance should be bounded by time or completed-review
count. Record lifecycle/publication kinds, successor count, saved provider work,
deadline margin before each Reconcile, latency, complete accounting,
publication identity, and alarm/event-source health. Absence of a natural
lifecycle transition is reported as unexercised rather than manufactured.

## Paid validation

Paid provider tests are never part of ordinary contributor CI. A maintainer must opt in deliberately, freeze an exact public head, use the isolated local DRY_RUN harness, reconcile every provider call/token/cost, and prove zero GitHub/AWS product writes. Validation evidence must stay in an explicitly chosen local, access-controlled destination.

The deployed Pipeline's `DRY_RUN` environment flag is different: it suppresses GitHub publication but retains AWS recovery and accounting state. See [configuration](CONFIGURATION.md#dry_run) before using either path.

Never paste private source, secrets, raw provider payloads, installation IDs, or private logs into tests, issues, Discussions, or pull requests.
