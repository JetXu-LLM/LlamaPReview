"""Sanitized behavior replays at the current presentation boundary.

These fixtures preserve only public structural facts needed to prove that a
correct Deep/Final judgment is not lost to anchor, evidence, visibility, or
inline normalization. They contain synthetic repository identities and make no
provider-quality claim.
"""

import unittest

from tests.unit.fakes import (
    ensure_repo_root_on_path,
    install_fake_requests_module,
    set_default_env,
)

ensure_repo_root_on_path()
set_default_env()
install_fake_requests_module()

from lambdas.LlamaPReviewPipeline.review.presentation import (
    PRESENTATION_VERSION,
    compile_presentation_v1,
)


HEAD = "b" * 40


def _meta(path, *, extra_path, risk_domains):
    return {
        "head_sha": HEAD,
        "analyzer_result": {
            "pr_type": "code",
            "risk_domains": list(risk_domains),
        },
        "fetch_health": {"status": "healthy"},
        "evidence_catalog": [
            {
                "id": f"path:{path}",
                "source_type": "diff",
                "outcome": "hit",
                "paths": [path],
                "coverage_type": "changed_region",
            },
            {
                "id": "ev_exact_contract",
                "source_type": "file",
                "outcome": "hit",
                "paths": [extra_path],
                "coverage_type": "full_file",
                "source_ref": f"pr_head:{HEAD}",
            },
        ],
    }


def _presentation(*, verdict, finding=None, unknown=None):
    summaries = {
        "clear": (
            "No review blocker found. The changed behavior and its retained "
            "contract were checked."
        ),
        "verification_needed": (
            "Verify the unresolved producer contract before merging."
        ),
        "blocking": (
            "Do not merge until the changed behavior preserves its required "
            "contract."
        ),
    }
    return {
        "version": PRESENTATION_VERSION,
        "decision": {
            "verdict": verdict,
            "confidence": "High",
            "summary": summaries[verdict],
            "owner_actions": [],
        },
        "findings": [] if finding is None else [finding],
        "material_unknowns": [] if unknown is None else [unknown],
        "confidence_checks": [],
        "diagram": None,
    }


def _finding(
    *,
    finding_type,
    headline,
    path,
    snippet,
    comment,
    priority="P1",
    placement="inline",
):
    return {
        "headline": headline,
        "priority": priority,
        "category": finding_type,
        "confidence": "High",
        "file_path": path,
        "code_snippet": snippet,
        "analysis": comment,
        "owner_action": (
            "Keep the changed boundary from reaching the unsafe consequence."
        ),
        "required_evidence_refs": [f"path:{path}", "ev_exact_contract"],
        "supporting_evidence_refs": [],
        "placement": placement,
        "suggestion": None,
    }


class SemanticReplayTests(unittest.TestCase):
    def test_causal_finding_survives_and_gets_one_inline(self):
        path = "services/order-router/routes/orders.js"
        snippet = (
            "const alpacaFirst = preferredBroker(uid, req) !== 'ibkr' "
            "&& alpaca.available(uid);"
        )
        details = f"""# Pull Request #101

## File Changes
### {path}
```diff
@@ -145,2 +145,3 @@
+      {snippet}
       const attempts = alpacaFirst
```
"""
        meta = _meta(
            path,
            extra_path="services/order-router/lib/trading-api-bridge.js",
            risk_domains=["state"],
        )
        finding = _finding(
            finding_type="bug",
            headline="A broker exception can fall through to the other account",
            path=path,
            snippet=snippet,
            comment=(
                "The changed broker selection can reach a producer that rejects; "
                "the retained catch maps that rejection to the same null sentinel "
                "as not connected, so the loop may attempt the other broker."
            ),
        )

        compiled = compile_presentation_v1(
            _presentation(verdict="blocking", finding=finding),
            pr_details=details,
            context_meta=meta,
        )
        self.assertTrue(compiled.publishable)
        review = compiled.review["v3_review"]

        self.assertEqual(review["decision"]["verdict"], "blocked_findings")
        self.assertTrue(review["findings"][0]["blocking"])
        self.assertEqual(review["findings"][0]["visibility"], "inline")
        self.assertEqual(len(compiled.review["inline_comments"]), 1)

    def test_open_producer_contract_cannot_compile_clear(self):
        """Replay the provider-observed conservative Final projection.

        The saved Final kept the outcome-merger candidate but could not
        verify the producer throw contract. In the current boundary, Final
        presents that candidate as non-blocking and carries the unresolved
        precondition as the model-owned merge-affecting unknown. Code must not
        derive clear from it.
        """

        path = "services/order-router/routes/orders.js"
        snippet = (
            "let result = null;\n"
            "for (const attempt of attempts) {\n"
            "  result = await attempt().catch(() => null);\n"
            "  if (result) break;\n"
            "}"
        )
        details = f"""# Pull Request #101

## File Changes
### {path}
```diff
@@ -145,4 +145,5 @@
+      {snippet.replace(chr(10), chr(10) + '+      ')}
```
"""
        meta = _meta(
            path,
            extra_path="services/order-router/lib/broker-adapter.js",
            risk_domains=["state"],
        )
        finding = _finding(
            finding_type="bug",
            headline="Thrown producer errors can select the alternate broker",
            path=path,
            snippet=snippet,
            comment=(
                "The changed attempt boundary maps a thrown producer outcome "
                "to the same sentinel as not connected, so the existing loop "
                "can select the alternate broker."
            ),
            priority="P2",
            placement="collapsed",
        )
        unknown = {
            "missing_fact": (
                "Whether the selected producer and every callee are throw-free."
            ),
            "impact": (
                "A thrown outcome would be merged with not-connected and may "
                "select the alternate broker."
            ),
            "owner_action": "Inspect the complete producer terminal contract.",
            "evidence_refs": [f"path:{path}", "ev_exact_contract"],
        }

        compiled = compile_presentation_v1(
            _presentation(
                verdict="verification_needed",
                finding=finding,
                unknown=unknown,
            ),
            pr_details=details,
            context_meta=meta,
        )
        self.assertTrue(compiled.publishable)
        review = compiled.review["v3_review"]

        self.assertEqual(review["decision"]["verdict"], "unverified")
        self.assertFalse(review["findings"][0]["blocking"])
        self.assertEqual(review["findings"][0]["priority"], "P2")
        self.assertTrue(review["material_unknowns"][0]["affects_merge"])

    def test_authority_finding_survives_inline(self):
        path = "src/main.rs"
        snippet = (
            "autodiscover::resolve_user_display_name("
            "dir.as_ref(), &email_for_resolve)"
        )
        details = f"""# Pull Request #202

## File Changes
### {path}
```diff
@@ -90,2 +90,3 @@
+            {snippet}
         }})
```
"""
        meta = _meta(
            path,
            extra_path="src/autodiscover.rs",
            risk_domains=["security"],
        )
        finding = _finding(
            finding_type="security",
            headline="Requester-selected email can disclose a directory display name",
            path=path,
            snippet=snippet,
            comment=(
                "The changed lookup uses the request-selected email without "
                "evidence that it is bound to the authenticated principal, so "
                "the response can expose another directory identity."
            ),
        )

        compiled = compile_presentation_v1(
            _presentation(verdict="blocking", finding=finding),
            pr_details=details,
            context_meta=meta,
        )
        self.assertTrue(compiled.publishable)
        review = compiled.review["v3_review"]

        self.assertEqual(review["decision"]["verdict"], "blocked_findings")
        self.assertEqual(review["findings"][0]["finding_type"], "security")
        self.assertEqual(review["findings"][0]["visibility"], "inline")
        self.assertEqual(len(compiled.review["inline_comments"]), 1)

    def test_clear_control_does_not_force_a_finding_or_inline(self):
        path = "src/control.js"
        details = f"""# Pull Request #303

## File Changes
### {path}
```diff
@@ -1,1 +1,1 @@
-const enabled = false;
+const enabled = true;
```
"""
        meta = _meta(
            path,
            extra_path=path,
            risk_domains=[],
        )

        compiled = compile_presentation_v1(
            _presentation(verdict="clear"),
            pr_details=details,
            context_meta=meta,
        )
        self.assertTrue(compiled.publishable)

        self.assertEqual(
            compiled.review["v3_review"]["decision"]["verdict"],
            "clear",
        )
        self.assertEqual(compiled.review["v3_review"]["findings"], [])
        self.assertEqual(compiled.review["inline_comments"], [])

    def test_second_clear_control_remains_concise_and_unforced(self):
        path = "src/reviewed.rs"
        details = f"""# Pull Request #404

## File Changes
### {path}
```diff
@@ -1,1 +1,1 @@
-const ENABLED: bool = false;
+const ENABLED: bool = true;
```
"""
        meta = _meta(
            path,
            extra_path=path,
            risk_domains=[],
        )

        compiled = compile_presentation_v1(
            _presentation(verdict="clear"),
            pr_details=details,
            context_meta=meta,
        )
        self.assertTrue(compiled.publishable)
        review = compiled.review["v3_review"]

        self.assertEqual(review["decision"]["verdict"], "clear")
        self.assertEqual(review["findings"], [])
        self.assertEqual(compiled.review["inline_comments"], [])
        self.assertNotIn(
            "Owner action:",
            compiled.review["pr_review_comment"],
        )

    def test_contradicted_rename_hypothesis_stays_dropped(self):
        path = "src/rename.py"
        snippet = "target.replace(source)"
        details = f"""# Pull Request #505

## File Changes
### {path}
```diff
@@ -1,1 +1,1 @@
-source.rename(target)
+{snippet}
```
"""
        meta = _meta(
            path,
            extra_path=path,
            risk_domains=[],
        )
        contradicted_headline = (
            "Windows rename may fail when the destination exists"
        )

        compiled = compile_presentation_v1(
            _presentation(verdict="clear"),
            pr_details=details,
            context_meta=meta,
        )
        self.assertTrue(compiled.publishable)

        self.assertEqual(
            compiled.review["v3_review"]["decision"]["verdict"],
            "clear",
        )
        self.assertEqual(compiled.review["v3_review"]["findings"], [])
        self.assertEqual(compiled.review["inline_comments"], [])
        self.assertNotIn(
            contradicted_headline,
            compiled.review["pr_review_comment"],
        )


if __name__ == "__main__":
    unittest.main()
