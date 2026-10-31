# Direct OpenAI stage profile

## Status and activation boundary

The adapter and stage table are implemented for **offline qualification**. This
change neither selects a production winner nor authorizes paid testing or
production deployment. Actual grant balance/expiry, deployed version, current
review volume and model quality/cost selection remain unverified.

`MODEL_PROVIDER=direct_openai` is an explicit provider choice. The existing
OpenRouter default and explicit DeepSeek profile remain compatible; there is
no automatic provider fallback. Direct mode also requires an explicit
`DIRECT_OPENAI_PROFILE`, along with the adapter's separate durable budget and
account-qualification gates. Merely supplying an API key or selecting a profile
is not proof of permission, available credits, or qualification.

[`config/direct_openai_profile.example.json`](../config/direct_openai_profile.example.json)
is a **not-qualified all-Luna-max comparison baseline**, not the selected
production profile. It cannot be treated as evidence of good quality or a
$150 monthly forecast. After separate authorization and actual comparisons,
replace individual cells only where the evidence supports the model/effort;
version and freeze that exact table with the selection report and release.

The environment value is compact JSON with these required fields:

- `version`: a nonempty, bounded identifier;
- `route`: `[exact_model_id, effort]`, used before a tier is known;
- `tiers`: exactly `low`, `normal`, and `high`, with the stage cells below.

An optional `reconcile_cache` field accepts only `off` (the default when absent)
or `same_run_30m`. Adding this opt-in binds a new canonical profile digest; it
requires separate qualification and a matching approved budget policy. The
shipped example remains unchanged and cache-off. Missing-field profiles retain
their original digest and request bytes. Explicit `off` also uses the original
cache-off wire, but, as with any explicit profile-field change, has its own digest.
An independent optional `static_prefix_cache` field accepts only `off` (absent
by default) or `public_static_30m_v1`. It permits only the versioned public
Route developer constant (also used by continuation Plan), and Deep's literal
public template prefix before its first PR field. It never shares PR content,
changes the selected models, or enables Final/representation-repair caching.
Absent profiles keep the old digest and wire; explicit opt-in requires a newly
authorized full profile digest and separate qualification.

See [the bounded cache contract](DIRECT_OPENAI_BUDGET.md#opt-in-same-run-reconcile-prefix).

The JSON parser rejects duplicate keys, omitted or extra cells, aliases,
unknown models, invalid efforts, fallback fields and oversized configuration.
The parsed profile is immutable. Its canonical SHA-256 makes a version label
with changed contents distinguishable in dispatch accounting. Keep the complete
Lambda environment under its platform limit; the parser's per-value bound does
not guarantee that other configuration and credentials fit.

## Existing stages, not a new routing system

| Existing Route tier | Payable stage cells |
| --- | --- |
| Not yet classified | `route` (global cell) |
| `skip` | None after Route |
| `low` | `deep`, `final` |
| `normal` | `pfr_plan`, `pfr_reconcile`, `deep`, `final` |
| `high` | `pfr_plan`, `pfr_reconcile`, `deep`, `final` |

The existing Route contract still determines the tier. No diff-size classifier,
repository/keyword exception, confidence-triggered escalation, second reviewer
or successful-path call is added. Existing Route adjudication reuses the Route
cell. PFR representation repair reuses the Reconcile cell. Final representation
correction reuses the Final cell, even if Deep used a different model. These
are existing bounded corrections, not a model-upgrade chain.

In direct mode the table alone selects a stage's model and effort; the legacy
DeepSeek `risk_domains` plan-model bump does not override that selection.
Fallback/degraded evidence and nonpublishable outcomes keep their existing
owners and cannot create a synthetic clear result.

Allowed exact IDs/efforts are:

- `gpt-6-luna`: `medium`, `high`, `xhigh`, `max`;
- `gpt-6.1-terra`: `low`, `medium`, `high`, `xhigh`, `max`.

Luna high/xhigh are explicit supported alternatives, not automatic upgrades or
fallbacks. Their availability follows the [official Luna model reference](https://developers.openai.com/api/docs/models/gpt-6-luna); each still needs stage-specific
qualification at its proposed output cap and deadline. No profile example or
production selection changes merely because the parser accepts the effort.

Being syntactically allowed is **not** evidence that a cell is qualified. Terra
medium/high are the initial comparison candidates; other efforts require the
bounded follow-on case described in the evaluation plan. No Sol or future Luna
ID is accepted. A future model needs explicit code/config changes, verified
account/API/pricing semantics, and renewed qualification; no alias upgrade is
automatic.

Each direct call supplies `profile_version`, `profile_sha256`, `stage`, and
`tier` in content-free trace metadata. Route's `tier` is the empty string because
Route must not assume its own answer. Dispatch accounting binds that selection
alongside the exact model and effort; recovery cannot rewrite a prior call as a
new selection.

## Bounded calls

Direct Route/adjudication requests cap output at 16,384 tokens; Plan and initial
Reconcile cap it at 12,000. Reconcile representation repair retains its existing
12,000-token cap. Deep and Final retain their configured caps (defaults 64,000
and 18,000). These are maximum output budgets **including reasoning**, not a
claim about actual token use. Successful-path call counts, retrieval rounds,
question limits, exact-head checks and publication behavior are unchanged.

Direct PFR retains the current reasoning-provider time bounds (600-second soft
limits and the configured hard limits), rather than silently inheriting the
legacy DeepSeek 180/240-second normal-profile bounds. All calls remain bounded
by the existing phase deadline and durable-state reserve.

## Offline evidence and next qualification

`tests/unit/test_direct_openai_profile.py` covers strict parsing and immutable
identity, stale legacy configuration, Route/adjudication inheritance, distinct
Plan/Reconcile choices in the existing conversation, separate Deep/Final
choices, real Final compilation into the GitHub body, and inherited bounded
correction calls. These use synthetic responses and no provider API.

Use [the evaluation and selection plan](EVALUATION_SELECTION.md) to prepare the
future bounded comparison. The final production profile must be supported by
paired evidence and a forecast using **actual current admitted traffic**;
daily capacity limits or historical token arithmetic are not monthly usage.
The desired approximately $150 allocation is an optimization goal with margin,
not a spending target or proof of a $200 usable grant.

