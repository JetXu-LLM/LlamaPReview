# Offline evaluation planning and selection

This is a planning contract, **not a live evaluation runner**. It does not
authorize provider calls, publication, deployment, or a production model
choice. No result, current traffic, account grant, or production identity is
asserted by the example manifest.

```bash
python scripts/validate_evaluation_plan.py
python -m unittest tests.unit.test_evaluation_plan
```

The validator reads only its named local JSON document. It imports no runtime,
provider, network, or subprocess code; it does not fetch repositories, load
credentials, inspect referenced artifacts, or write a receipt file. Its stdout
is a bounded structural receipt, always with `execution_permitted=false` and
zero calls/product writes. A zero exit status means **valid offline plan**, not
provider qualification or measured review quality.

## Plan and data boundaries

[`evaluation_plan.example.json`](../config/evaluation_plan.example.json) contains
synthetic, unmaterialized case placeholders. The twelve cases are a manageable
diagnostic starting set, not a statistical sample-size requirement. Three are
reserved for confirmation and are not scheduled in the screening batches.
Coverage tags organize evaluation; they are not production routing rules.

The example's token, call, and wall-time ceilings are illustrative planning
bounds, not measured needs or new production limits. The sum includes all
listed conditional comparisons and is a worst-case planning count, not a
command to run a Cartesian product. `max_repeats` bounds separate experimental
observations, not automatic transport retries. The proposed phase-call caps
preserve one Route/Plan/Deep/Final/correction and at most two Reconcile calls per
case/candidate observation. They do not increase the runtime's existing caps.

Materialize future case inputs only from permitted public repositories. Bind
each public snapshot to a canonical repository URL, full head SHA, and input
SHA-256, and bind the candidate source commit and locked SDK wheel separately.
The validator checks declaration shape; a GitHub URL is **not** proof of public
visibility, a digest is not proof that bytes were checked, and structural
validity is not permission to transmit anything. Verify provenance, exact-head
content, permissions, and hashes independently before any subsequently
authorized use. Do not put credentials, private source, installation IDs,
operational receipts, raw provider payloads, or traces in this repository.

The public plan remains `unapproved`, with approved calls and budget zero.
Future approval and financial limits belong in a separate private operator
receipt. This validator intentionally cannot certify that receipt or execute
it. The [paid-validation boundary](DEVELOPMENT.md#paid-validation) still applies.
Deployed `DRY_RUN` is only a GitHub write barrier, not a zero-AWS-write sandbox.

## Candidate policy

Use exact, explicitly checked model IDs. The present planning allowlist is
`gpt-6-luna` with `medium` or `max`, and `gpt-6.1-terra` with `low`, `medium`,
`high`, `xhigh`, or `max`. It excludes Sol, gateways, speculative model names,
and automatic model substitutions. This is an evaluation scope, not a claim
that every listed setting has been qualified on the target account.

Start Terra screening at medium/high. Add xhigh/max only when an unresolved
hard case could change the selection; compare low only as a follow-on for a
stage already shown to admit simplification. Official model documentation is
the source for current supported parameters and prices:
[Luna](https://developers.openai.com/api/docs/models/gpt-6-luna) and
[Terra](https://developers.openai.com/api/docs/models/gpt-6.1-terra).

| Existing phase | First comparison | Conditional follow-on | Evidence to inspect |
| --- | --- | --- | --- |
| Route, before a tier exists | Luna max / Terra medium | Terra high if consequential under-routing remains | Incorrect skip, missing deciding objective, necessary evidence lost through under-routing |
| Low Deep | Luna medium / max | Terra medium diagnostic reference | Local defects, clean controls, small high-consequence changes |
| Normal/high PFR Plan | Luna max / Terra medium / high | Higher Terra effort only for a demonstrated evidence-acquisition bottleneck | Acceptance criteria, callers/state prerequisites, validation execution chain |
| Normal/high Reconcile | Luna max / Terra medium / high | Higher effort for a concrete unresolved case | Contradictions, honest gaps, useful next bounded read |
| Normal/high Deep | Luna max / Terra medium / high | High-tier xhigh, then max if useful | Causality, materiality, severity, false positives, known misses |
| Low Final | Luna medium / max | Terra medium diagnostic reference | Concision without invented findings or lost judgment |
| Normal/high Final | Luna max / Terra medium | Terra high for material compression loss; low for demonstrated simplification | Deciding findings, causal references, first-screen action, schema and placement |
| Correction/repair | Inherit the qualified owning stage | No second reviewer or automatic paid escalation | Preserve judgment and existing bounded correction semantics |

The example intentionally schedules only an initial subset and a few optional
comparisons. Extend a local plan deliberately; do not interpret unscheduled
cells as passed. Do not add PFR work to a low path that has none. A small but
dangerous diff may be used as an adversarial low-stage diagnostic without
claiming that it belongs in the low production tier.

## Cheap, attributable comparisons

1. Run deterministic safety/replay checks before considering live work. Their
   success does not establish stochastic provider judgment quality.
2. Compare Route with identical initial PR facts. Compare Deep with identical
   evidence capsules, Final with identical Deep judgments and reference IDs,
   and Reconcile with identical acquired evidence. This isolates stage loss.
3. Evaluate PFR evidence acquisition separately: let candidates execute their
   own bounded plans against the same frozen head and tool contract. A stronger
   Deep cannot recover evidence that was never retrieved.
4. Repeat a disputed, selection-changing comparison within a separately
   authorized call/budget bound. Do not choose from one lucky finding or treat
   normal wording variance as a hard system regression.
5. After screening, compare the all-Luna-max baseline with the leading mixed
   profile through the complete production path in an isolated no-product-write
   harness. At most add a runner-up if the remaining question warrants it.
   Use normal Route selection; forced stage inputs alone cannot qualify a mix.
   Full-profile execution is intentionally not implemented by this validator.

Keep development and confirmation inputs separate. Later fixes, comments and
CI facts may inform adjudication but must not leak into earlier-head input.
Near-duplicate heads from one defect are not independent evidence. A case used
to tune prompts/profile selection becomes development material, not an unseen
confirmation case. Do not call the existing public `sealed` replay list a
hidden quality benchmark. Human/code/test evidence resolves consequential
disputes; a candidate's own assessment is not the sole oracle.

## Selection and cost evidence

Use the [selection report template](templates/EVALUATION_SELECTION_REPORT.md).
Keep supported material findings, known misses, false blockers, avoidable owner
work, delivery loss, latency and actual usage separate. Do not reward verbosity
or comments for their own sake. Distinguish an honest clear review, an honest
unknown, a skipped request, and a failed review. Likes, merges and later fixes
alone do not establish attributable maintainer benefit.

Reject hard system counterexamples first. Then remove dominated configurations
and spend more only where independently checked material benefit justifies it.
Where quality is indistinguishable, prefer the cheaper, faster, simpler option.
No arbitrary confidence threshold, line-count rule, aggregate score, second
reviewer, or keyword-owned engineering judgment is introduced. Selection is
among tested candidates, not a proof of global optimality.

Project monthly cost from actual admitted-run/tier frequencies and measured
complete-path costs, including paid failed/cancelled/superseded work, correction
and recovery, without double-counting retries or reasoning-token breakdowns.
Keep gross usage, credit-covered usage, cash expenditure, pending reservations,
and other projects' allocations distinct. Quota ceilings are not traffic
measurements. Unknown usage is not zero. Record a central estimate and a
stress scenario grounded in observed variation; do not manufacture a monthly
forecast from this template. A budget is a ceiling, not a target to spend up to.

## Configuration and future models

Keep any eventual production selection as a small versioned phase/tier profile
using the existing Route tiers, not a new classifier, shadow runtime or
repository rollout list. Log the selected model/effort at the existing dispatch
boundary. No automatic cash-funded provider fallback follows from this plan.

A future model name or external benchmark improvement is only a reason to
reevaluate. Verify official availability, parameters, pricing and the target
account first; then compare frozen capsules and the full selected profile.
Neither this manifest nor the validator automatically switches production.
