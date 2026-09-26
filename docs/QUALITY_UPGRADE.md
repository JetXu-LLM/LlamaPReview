# Review delivery and Luna migration

The upgrade aims to deliver supported findings to maintainers, reduce
unnecessary re-review when CI changes, and preserve honest provider accounting.
It uses the existing Route → PFR → Deep → Final → Projection → publication path.

The investigation combined a September 12 audit with a September 12–26
production window. Public examples include the unavailable review on
[Controlix #60](https://github.com/ErfanBagheri404/Controlix/pull/60#pullrequestreview-5323331503)
and the useful conditional review on
[Tranc3 #1232](https://github.com/Trancendos/Tranc3/pull/1232#pullrequestreview-5283855093).
A failure notice proves that delivery failed; it does not independently prove
that every rejected model finding was correct. Raw operational evidence and
provider traces remain private. No aggregate accuracy or adoption claim follows
from these diagnostic cases.

## Changes and acceptance

| Problem | Owning change | Observable acceptance |
| --- | --- | --- |
| Final loses a finding's causal references or emits invalid JSON | Deep identifies causal references; Final preserves them and may reuse an ID in a finding and a confidence check. One representation correction receives the original judgment and compiler diagnostics. | A supported finding reaches the rendered review; a correction cannot turn a parsed blocking verdict into empty clear. Invalid or stale evidence stays rejected. |
| CI changes discard an independent code judgment | Refresh current CI separately. Recompile locally when public CI facts change; use the existing bounded review retry only when deciding evidence or core prose no longer holds. | A new failed check is visible without fabricated code causality or merge safety. Resolving a deciding unknown asks the model to rejudge; code does not invent approval. |
| A completed in-flight call cannot settle after lifecycle changes | Require live ownership before dispatch, then settle only the exact existing call fence by compare-and-swap. | Late usage survives cancellation or takeover, duplicate settlement is a no-op, and the old owner cannot start another call or publish. |
| A trickling HTTP response exceeds the intended total time | Enforce the remaining wall budget around request and body decoding on the Lambda main thread, preserving the outer phase timer. | A local drip-response test expires one dispatch without a second purchase; missing usage remains unknown. |
| Test fixtures are mistaken for shared production behavior | Deep checks lexical scope, actual callers, and the purpose of negative controls. | A test-only mutation is not reported as a production regression without a real caller path. |
| Authors are asked to recheck available repository facts | Deep answers supplied manifest/helper facts, distinguishes unread evidence from external unknowns, and keeps optional improvements nonblocking. | Frozen source reviews resolve an admitted dependency range locally and retain honest gaps when evidence is absent. |
| A repository-tree timeout blocks reads of known PR paths | Reuse the existing bounded exact-head probes when inventory is unavailable. Leave the probe allowance for planned evidence instead of speculative owner-document reads. | Multiple planned paths can yield real file evidence while the whole-tree gap stays explicit; sensitive paths and the six-probe cap remain enforced. |
| A max-effort plan consumes the old retrieval window before useful reads run | Luna uses a 600-second soft retrieval gate within the existing 780-second context budget. The orchestrator passes the mode's soft gate explicitly; DeepSeek retains its prior budgets. | Normal and high overrides remain independent. Retrieval and reconciliation still share the hard deadline and state reserve; extra time is not evidence that the model found the right issues. |
| Model labels hide the actual transport | One provider profile selects OpenRouter `openai/gpt-6-luna` with `max` for every active model call. The ledger retains requested/returned identities and reported usage. | Route, PFR, Deep, Final, and correction use the same target; switching `MODEL_PROVIDER` to `deepseek` restores the previous profile. |

Evidence and lifecycle checks retain their existing owners. The change does not
add another reviewer, retrieval loop, publication surface, persistent judgment
schema, repository-specific rule, or automatic provider fallback. Successful
Final output pays no correction call. True CI changes may still require another
Deep/Final attempt; that is a semantic rejudgment, not a formatting retry.

## Validation boundaries

Synthetic tests exercise rendered findings and first-screen CI text, exact-head
rejection, accounting settlement, one-shot correction, and provider rollback.
The current replay manifest binds representative cases to these outcomes.
Python 3.11/3.12, release packaging, supply-chain checks, and reference Terraform
remain release gates.

Maintainer-only real-PR simulations run the exact clean public commit and locked
SDK through private tooling with frozen heads and zero GitHub/AWS product
writes. A Codex CLI Luna/max simulation can evaluate evidence acquisition,
judgment, and presentation. It cannot qualify OpenRouter's wire behavior,
gateway billing, model-serving identity, or per-call output-token enforcement.
Those differences stay explicit in its receipt. No OpenRouter connection test
is claimed for this upgrade.

The Luna default is a trial migration, not an established quality improvement.
Local CLI runs recovered supported lockfile, JSON parsing, and HTML-email
findings, but also showed variable findings and a reconciliation timeout on a
repeated frozen PR. A successful schema or publication receipt does not cancel
those misses. These diagnostic cases do not establish that Luna reliably
replaces Flash; the DeepSeek profile remains available through the same runtime.

The default in source is not a deployment receipt. Official activation still
requires a real OpenRouter key, verified public release artifacts, an inspected
private saved plan, and a separate explicit production action. See
[configuration](CONFIGURATION.md), [review output](REVIEW_OUTPUT.md), and
[release verification](RELEASE_VERIFICATION.md).
