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

Lifecycle changes additionally require deterministic coverage for the full
state matrix: same/new head crossed with open/merged/closed, plus an unverified
snapshot. Tests must prove durable initial admission, one bounded early
successor, silent late supersession, cancellation and post-merge rendering,
disposition-bound dispatch and recovery, complete predecessor accounting, and
unchanged ordinary open-PR output. Callback-order tests must place lifecycle
checks before the first Reconcile and before Final without changing the normal
number of bounded PFR rounds.

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
