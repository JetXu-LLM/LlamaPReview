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
`prompt_cache_options.mode=explicit` **without breakpoints**. The transport envelope caps plain text at 4MB UTF-8 (and 128 messages), so it
does not accidentally narrow the existing 850k-character stage cap for Unicode.
Existing stage character limits remain unchanged; this is not a promise that
every bounded input fits the provider context window. No provider tools,
remote conversations, SDK retries, redirects, model fallback, second reviewer,
confidence escalation, or new successful-path calls are enabled.

Route/adjudication has an 8,192-output-token cap; Plan and Reconcile have 12,000;
Deep, Final, and existing correction retain their existing configured caps.
Every retry is a separate existing `call_id` under the same `operation_id` and
must reserve its own maximum cost before HTTP. Refusal, incomplete/truncated
output, unsupported output items, malformed usage and errors cannot become a
clean review. Reasoning items are not exposed as review prose.

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

## Price evidence and qualification limits

The frozen standard non-regional text rates (USD per million tokens) are:

| Model | Input | Cached input | Output |
| --- | ---: | ---: | ---: |
| `gpt-6-luna` | 0.10 | 0.01 | 0.50 |
| `gpt-6.1-terra` | 1.00 | 0.05 | 5.00 |

Inputs over 272K use 2× input/cache and 1.5× output rates for the whole request.
The adapter requests no caching; it does not assume future savings. See official
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
