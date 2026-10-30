# Direct OpenAI: bounded adapter and grant-window budget

**Implementation and offline verification only.** No model-quality winner,
account access, active credit grant, current hosted runtime, traffic volume,
monthly forecast, or production activation is established by this change.
Ordinary tests make no real model calls or GitHub/AWS product writes. The
existing provider default is unchanged; there is no automatic paid fallback.

## Explicit activation contract (not deployment instructions)

The opt-in provider is `MODEL_PROVIDER=direct_openai`. It additionally requires:

- `OPENAI_API_KEY` for the explicitly verified organization/project;
- `DIRECT_OPENAI_PROFILE`: the compact exact stage table described in
  [profile configuration](DIRECT_OPENAI_PROFILE.md);
- `DIRECT_OPENAI_BUDGET`: an operator-approved grant-window allocation below.

The shipped [budget example](../config/direct_openai_budget.example.json) is
intentionally invalid for dispatch: authorization is `unapproved` and unknown
facts are null. It is not a free `$200` reset or a ready production configuration.
The [evaluation plan](EVALUATION_SELECTION.md) cannot execute any request.

| Budget field | Meaning |
| --- | --- |
| `authorization` | Must be the explicit `approved` assertion; never inferred from a subscription or this example. |
| `cycle_id` | Stable identifier of one verified allocation/grant window, not a wall-calendar month. |
| `valid_from`, `expires_at` | Exact UTC epoch seconds for the verified usable grant. |
| `allocation_nanousd` | Application ceiling, integer nano-USD, at most 150,000,000,000 ($150). It is a stop limit, not a spending target. |
| `verified_available_nanousd` | Verified amount available to this allocation, after other commitments and reserves; cannot be less than the application ceiling. |
| `organization_id`, `project_id` | Exact account scope sent in the corresponding OpenAI headers. Keep real values in private operator configuration. |
| `profile_sha256` | Canonical digest of the profile specifically qualified and authorized for this allocation. |
| `pricing_version` | Exact code-bound snapshot `openai-standard-text-2026-10-29`. |
| `pricing_verified_until` | Explicit UTC expiry for verified pricing, no later than operators can responsibly support. |

Unknown, missing, malformed, duplicate-key, expired, oversize-budget or
unqualified configuration stops new purchases. Each attempt must fit wholly
within both grant and price windows, including its HTTP timeout plus 30 seconds.
No next grant is assumed to exist. A new allocation needs new verified facts and
authority; changing `cycle_id`, project or organization to regain capacity from
the **same** spent grant is forbidden. The counter binds the complete immutable
policy digest, so changing a profile, cap or policy field mid-cycle does not
silently reset or enlarge it. The admitted run also pins provider/profile;
a resumed stage cannot adopt a changed model table.

Before any activation, operators must separately verify grant ownership,
remaining balance/expiry, account model access, disjoint allocation from other
organization use, global/non-regional standard pricing, payment/auto-reload
settings, and platform limits. The app does not query billing or mutate account
settings. Its local ceiling **cannot guarantee organization-wide zero cash
spend** if other consumers can spend the same credits. A provider response using
an unexpected model identity, service tier or cache behavior is not accepted as
reconciled merely because it contains numbers.

## One transport, no hidden extra purchases

`DeepSeekClient` retains its compatibility name but owns the sole HTTP attempt
loop for all providers. The direct dialect uses `/v1/responses`, plain text only,
explicit reasoning, bounded output, `store=false`, `stream=false`,
`background=false`, `truncation=disabled`, `service_tier=default`, and
`prompt_cache_options.mode=explicit` **without breakpoints by default**. The transport envelope caps plain text at 4MB UTF-8 (and 128 messages), so it
does not accidentally narrow the existing 850k-character stage cap for Unicode.
Existing stage character limits remain unchanged; this is not a promise that
every bounded input fits the provider context window. No provider tools,
remote conversations, SDK retries, redirects, model fallback, second reviewer,
confidence escalation, or new successful-path calls are enabled.

Route/adjudication has a 16,384-output-token cap; Plan and Reconcile have 12,000;
Deep, Final, and existing correction retain their existing configured caps.
Every retry is a separate existing `call_id` under the same `operation_id` and
must reserve its own maximum cost before HTTP. Refusal, incomplete/truncated
output, unsupported output items, malformed usage and errors cannot become a
clean review. Reasoning items are not exposed as review prose.

Typed provider billing/quota rejection is terminal, not transient throttling.
HTTP402 and documented direct-OpenAI billing/quota429 codes stop after that
attempt; ordinary rate-limit429 retains bounded backoff. The original HTTP
status is preserved and unknown usage stays reserved. No error-message keyword
matching or body logging is used. Budget/admission failures stop the direct run;
PFR/Final cannot convert them into a repair or additional paid stage.

## Atomic reservation and exact-once settlement

Before each actual HTTP attempt, one DynamoDB transaction writes:

1. the existing owner/head/run-bound per-call dispatch fence;
2. a small aggregate counter for that exact allocation window in the same table.

The counter has amounts, policy identity and window metadata only—no growing
monthly call-ID collection. The original per-call record owns idempotency.
There is no second provider ledger or new table/service. A failed or ambiguous
transaction cannot cross HTTP. An unresolved prior dispatch cannot be bought
again on redelivery.

Amounts are integer nano-USD. Reservation uses the model's entire documented
1,050,000 input-token context limit at the long-context input rate, plus the
requested maximum output (including reasoning) at the long-context output rate.
This intentionally conservative bound avoids a paid token-count request and
unproven chars/token estimates. It can pause new work earlier than expected at
the end of an allocation; it does not claim the reserved amount was spent.

For valid usage, a second transaction replaces the exact original fence with its
terminal record and converts its reserve into token-priced cost. It uses the
**original durable cycle, amount and price**, never terminal-callback refund
coordinates or current environment configuration. A late response may settle
after cancellation, takeover, or expiry without regaining dispatch or publication
rights. Duplicate settlement is a no-op. Input/output totals, cache detail,
reasoning subset, model and service tier are checked. Output tokens already
include reasoning and are charged once. With no cache breakpoints, nonzero cache
reads/writes are a contract mismatch and retain the full bound, not an assumed
discount. Cached/write fields remain visible in content-free usage for diagnosis.

Timeout, transport/HTTP failure, missing usage, process death or price ambiguity
retains the entire reservation. A late/uncertain bill is never interpreted as
zero. The cycle counter has **no TTL**; existing PR/source retention is unchanged.
If its original per-call fence expires under normal PR TTL before reconciliation,
that reservation becomes permanently conservative in the counter: a missing
original can never be refunded automatically. Operators must not delete/reset a
counter to clear these obligations. A later verified grant does not erase old
ones or prove shared credits are unspent.

## Opt-in same-run Reconcile prefix

`reconcile_cache: "same_run_30m"` in the strict profile enables only the initial
Reconcile calls in existing rounds 1 and 2. It does not select a model, add a
round/call, change a prompt, or enable a deployment. This option alone leaves
Route, Plan, Deep, Final and representation corrections cache-off. The approved profile digest must
change; old qualification approvals are not authority for this new wire.

The trusted Reconcile builder keeps its own prefix through its generated
`</VERIFICATION_PLAN>` boundary separate from the remaining evidence. The direct
adapter sends exactly two `input_text` blocks in the same user message, with one
explicit breakpoint after the prefix and a request-level `ttl: "30m"`. Concatenating
them reproduces the original text exactly. Canonical messages remain strings, so
repair cannot inherit the breakpoint. No delimiter is searched in PR text.

An opaque `prompt_cache_key` binds the existing validated installation, repository
owner/ID, repository/PR, exact head, run, model/effort/profile, text format, developer/prefix
hash, and the verified budget organization/project. Repository IDs come only
from the already-fetched native PR; no new GitHub lookup or credential is added.
Missing or invalid scope keeps the old cache-off request. The durable fence owner
independently checks the approved profile, round, actual wire and admission;
only hashes and billing scalars persist, never raw cache scope or prompt text.

Every attempt reserves the full maximum input at the **cold 30-minute write**
rate, including when a hit is expected. At 12,000 output tokens the reserve is
$2.715 for Terra / $0.2715 for Luna; at 64,000 it is $3.105 / $0.3105. Insufficient
authorized headroom stops before HTTP rather than increasing the allocation.
Settlement validates raw usage before lossy numeric normalization, then charges
ordinary input, cache reads, and `ttl_30m_tokens` writes from the original fence's
price. Both TTL fields must be present and sum to writes; `ttl_12h_tokens` must
be zero. Missing, negative, boolean, fractional or contradictory partitions keep
the full reservation. Reasoning remains a subset of output. The **whole** input,
including all cache partitions, selects the >272,000 long-context price tier.

There is no token estimator or count API here. A prefix below the provider's
1,024-token minimum may simply not cache; character/byte counts do not prove that
minimum. No padding, prewarming, 12-hour retention, cross-run reuse or additional
request is permitted. Thirty minutes is a minimum retention, not a deletion
promise. A missing second round or cache miss can cost more than cache-off;
actual first-write plus second-read usage and quality/latency must be measured
under separately approved qualification before claiming savings.

## Opt-in public static templates

`static_prefix_cache: "public_static_30m_v1"` is an independent, default-off
profile option. Its versioned allowlist contains only already-public product
constants: the exact Route developer message (including Route adjudication and
four-message continuation Plan), and the exact Deep developer plus literal user
prefix before the first `pr_details` field. The latter boundary comes from the
code-owned format template, never a tag search or empirical common-prefix cut.
Canonical role/order/text/options and call counts are unchanged. Standalone
Plan, unknown/custom templates, Final and representation corrections stay off.

The single explicit breakpoint can share **only these public constants** across
runs/repositories/installations in the verified OpenAI organization/project.
No PR, repository, tenant, head, run, evidence, or customer-specific byte occurs
before this public boundary. Reconcile's PR-bearing prefix remains in its
separate same-run namespace and retains its scope/round checks.

The opaque public key binds strategy/version, public-prefix kind/hash, verified
organization/project, actual model/effort and text format. It deliberately does
not include tenant identities or the whole profile digest: unrelated stage edits
do not change an identical public prefix. This reuse key is **not authorization**;
each dispatch still binds the entire approved profile, operation, current owner,
head/run and budget. Transport and the original durable fence independently
recompute the public proof from the actual wire. No extra identity read, SDK
request, credentials, counter, implicit suffix write or prewarming is added.

The same original-fence cold-write reservation and strict raw partition checks
apply. At 8,192 output tokens Terra reserves $2.68644 and Luna $0.268644; 12k and
64k bounds are unchanged from the 30m table above. Unknown usage keeps its full
hold; late settlement uses the original price/cycle even after profile changes.
The provider's 1,024-token minimum is not proved by character/byte counts; short
prefixes may not cache and are never padded. No real cache hit, latency, quality,
monthly saving or production enablement is established by offline tests. Live
qualification requires separate explicit budget/run authority.

## Price evidence and qualification limits

The frozen standard non-regional text rates (USD per million tokens) are:

| Model | Input | Cached input | Output |
| --- | ---: | ---: | ---: |
| `gpt-6-luna` | 0.10 | 0.01 | 0.50 |
| `gpt-6.1-terra` | 1.00 | 0.05 | 5.00 |

Inputs over 272K use 2× input/cache and 1.5× output rates for the whole request.
The default adapter requests no caching; opt-in caching never assumes a hit or future savings. See official
[Luna](https://developers.openai.com/api/docs/models/gpt-6-luna),
[Terra](https://developers.openai.com/api/docs/models/gpt-6.1-terra),
[Responses](https://developers.openai.com/api/reference/python/resources/responses/methods/create),
and [prompt caching](https://developers.openai.com/api/docs/guides/prompt-caching)
documentation (checked 2026-10-29). These are token-priced accounting amounts,
not proof of the account invoice, grant debit, future price or real task quality.
Unknown future model IDs, including any future Luna version, remain rejected
until explicitly configured, priced, qualified and authorized in a later change.

Production credentials plus profile and grant JSON must fit the Lambda 4KB
environment limit. Public examples can be size-checked offline; the actual secret-
bearing environment must be checked privately before any separately approved
deployment. No real environment values or deployment claims belong here.
