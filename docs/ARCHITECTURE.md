# Architecture

LlamaPReview has one production path:

```text
verified GitHub webhook
→ public/private eligibility
→ exact-head admission
→ Route
→ bounded exact-head retrieval / PFR
→ Deep engineering judgment
→ Final presentation
→ deterministic Projection and local degradation
→ exact-head, idempotent GitHub publication
```

![LlamaPReview production architecture](assets/architecture.svg)

## Capability ownership

| Capability | Owns | Does not own |
| --- | --- | --- |
| Webhook admission | Signature verification, event eligibility, the hosted public-only boundary, minimum exact-head queue input | Retrieval, model work, publication |
| Retrieval / PFR | Bounded repository evidence, provenance, coverage, and explicit gaps | Findings or merge posture |
| Deep | Causal engineering judgment, severity, uncertainty, and merge posture | Markdown layout or GitHub placement |
| Final | Compression, organization, owner-action language, inline requests, diagram presentation | Evidence retrieval or deterministic sanitation |
| Projection / rendering | Public schema, stable identities, caps, syntax, sanitation, local optional-surface degradation, GitHub payload shape | Engineering judgment |
| Provider transport / accounting | Dispatch fences, retries, usage, logical and billed model identity, tokens, trace, and cost truth | Review policy |
| Persistence / publication | Durable phase ownership, exact-head recovery, intent and receipt, exactly-once external effects | Workflow sequencing or model decisions |
| Orchestrator | Deadlines, sequencing, and terminal flow | A second copy of any capability above |

## Trust boundaries and invariants

### Private hosted events

The Webhook verifies the signature, reads event type and repository visibility, and acknowledges a private-repository event before it constructs a pipeline item. The application performs no DynamoDB or S3 write, provider call, GitHub product API call, or identity-bearing application log for that event.

### Exact-head evidence

The queued head SHA is not treated as sufficient proof. The Pipeline rereads the pull request lifecycle and head before context work, before review work, and before publication. Repository reads are pinned to that head where GitHub supports an exact ref. Evidence records carry provenance and coverage rather than silently promoting search hints to facts.

### Model and code boundaries

The model owns engineering judgment. Code owns bounded inputs, sensitive-path exclusions, schema validation, sanitation, output limits, placement, payload construction, durable state, and publication identity. If one optional inline placement or Mermaid surface is invalid, deterministic projection may degrade that surface locally without inventing a new judgment.

### Exactly-once publication

Before a GitHub write, the Pipeline stores an immutable publication candidate and an owner-bound intent. After dispatch it reconciles the exact head, payload digest, bot identity, and returned GitHub identifiers. Retries reuse the durable candidate or receipt; they do not regenerate and blindly post a second review.

### Accounting truth

Each provider HTTP attempt has a durable dispatch fence and a stable operation identity. The ledger retains logical routing identity, billed transport identity, status, token classes, and usage. A successful review cannot make a discarded or retried provider call disappear from accounting.

## Repository map

```text
lambdas/LlamaPReviewWebhookHandler/   signed public-only admission adapter
lambdas/LlamaPReviewPipeline/         active review runtime
  context_engine/                     bounded exact-head retrieval and PFR
  review/                             judgment, presentation, projection, rendering, publication
infra/terraform/                      generic AWS reference deployment
scripts/                              deterministic build and verification tools
tests/                                unit, replay, adversarial, privacy, and recovery contracts
docs/                                 current public operating and contributor documentation
```

Large modules are kept when they have one stable owner and a cohesive invariant set. File size alone is not an architectural reason to introduce forwarding facades, generic provider protocols, or a second lifecycle.
