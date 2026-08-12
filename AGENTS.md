# AGENTS.md

## Purpose

This public repository is the sole active implementation of the hosted
LlamaPReview Webhook and Pipeline. Runtime behavior, prompts, retrieval
integration, model routing, Projection, accounting, persistence, recovery,
publication, tests, release packaging, and generic self-hosting infrastructure
change here.

Official AWS production consumes exact attested semantic-release artifacts from
this repository through a separate private operations boundary. Public CI never
receives official AWS credentials and never deploys official production.

## Read Before Changing Product Behavior

1. `README.md`
2. `docs/ARCHITECTURE.md`
3. `docs/DEVELOPMENT.md`
4. `docs/REVIEW_OUTPUT.md`
5. `docs/SECURITY.md` and `SECURITY.md`
6. `docs/RELEASE_VERIFICATION.md`
7. `docs/HOSTING.md` and `docs/AWS_DEPLOYMENT.md` when release or deployment
   contracts are involved

## One Active Production Path

```text
verified GitHub webhook
-> public/private eligibility
-> exact-head admission
-> Route
-> bounded exact-head retrieval / PFR
-> Deep engineering judgment
-> Final presentation
-> deterministic Projection and local degradation
-> exact-head, idempotent GitHub publication
```

Capability ownership is fixed:

- Webhook admission owns signature verification, event eligibility, the hosted
  public-only boundary, and the minimum exact-head queue input.
- Retrieval/PFR owns bounded evidence, provenance, coverage, and honest gaps.
- Deep owns causal engineering judgment, severity, uncertainty, and merge
  posture.
- Final owns compression, organization, owner-action language, inline requests,
  and optional diagram presentation.
- Projection/rendering owns schema, stable identities, caps, sanitation,
  placement, optional-surface degradation, and GitHub payload shape.
- Provider transport/accounting owns dispatch fences, retries, logical and
  billed model identity, tokens, trace, usage, and cost truth.
- Persistence/publication owns durable recovery, exact-head idempotency,
  publication intents/receipts, reconciliation, and exactly-once effects.
- The orchestrator sequences these capabilities and owns deadlines/terminal
  flow; it must not become a second domain system.

Fix a defect in the capability that owns it. Change `llama-github` only when
executable evidence proves the independently released SDK owns the failure;
then update this repository's locked wheel identity and rebuild the public
Layer. Do not implement a second SDK behavior here.

## Change And Test Contract

- Behavior changes require focused unit or adversarial coverage and a
  representative replay when Route, retrieval, Deep, Final, Projection,
  placement, accounting, persistence, recovery, or publication changes.
- Preserve exact-head admission/rereads, sensitive-path exclusion, deterministic
  payload safety, complete provider accounting, idempotent publication, local
  optional-surface degradation, and zero-side-effect private-event discard.
- Ordinary tests use fakes, synthetic inputs, or redacted fixtures. They make no
  paid provider call and no GitHub/AWS product write.
- Paid qualification requires explicit maintainer intent, a clean frozen public
  commit and exact PR heads, complete call/token/model/cost reconciliation, and
  proof of zero GitHub/AWS product writes. Private traces and credentials stay
  outside this repository.
- Run the relevant `make test`, `make replay`, `make verify`, deterministic
  packaging, credential/history/artifact scans, dependency/license checks, and
  generic Terraform validation described in `docs/DEVELOPMENT.md`.
- Public CI is the clean-environment authority. A semantic release must bind one
  exact tag and commit to deterministic Function/Layer assets, checksums, SBOM,
  dependency/license inventory, and GitHub provenance.

## Public And Private Boundaries

Do not commit official AWS identities, private Terraform state, saved plans,
tfvars, environment projections, credentials, installation identities, raw
provider payloads, private traces, deployment receipts, generated release ZIPs,
or private repository source.

Do not introduce:

- repository-specific review heuristics or keyword-owned engineering judgment;
- a duplicate provider/accounting, lifecycle, publication, or recovery path;
- legacy routing, shadow/canary modes, repository rollout lists, or a
  private-source fallback;
- a runtime mirror, submodule, vendored private source, or synchronization
  workflow;
- automatic official-production deployment from public CI.

Public semantic versions and AWS Lambda numeric versions are distinct. This
repository publishes the semantic release; official operators independently
record the numeric AWS versions that consume it.

During a production incident, operators may roll AWS back to an already
verified deployment. The corrective product change still begins here, passes
public CI, and ships as a new semantic release. Never recreate an emergency
runtime from historical private source.
