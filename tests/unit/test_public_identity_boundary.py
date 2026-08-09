import unittest

from tests.unit.fakes import ensure_repo_root_on_path, install_fake_requests_module, set_default_env

ensure_repo_root_on_path()
set_default_env()
install_fake_requests_module()

from lambdas.LlamaPReviewPipeline.review.public_boundary import (
    collect_private_identities,
    sanitize_public_prose,
    sanitize_review_for_publication,
)
from lambdas.LlamaPReviewPipeline.review.presentation import (
    compile_presentation_v1,
)


class PublicIdentityBoundaryTests(unittest.TestCase):
    def test_exact_private_identities_are_removed_without_fuzzy_prefix_matching(self):
        identities = {"ev_exact", "res_unknown", "F1", "U1"}
        sanitized, count = sanitize_public_prose(
            "Observed `ev_exact` (res_unknown); keep ev_public_api and F10 unchanged.",
            identities,
        )
        self.assertEqual(count, 2)
        self.assertNotIn("ev_exact", sanitized)
        self.assertNotIn("res_unknown", sanitized)
        self.assertIn("ev_public_api", sanitized)
        self.assertIn("F10", sanitized)

    def test_citation_only_groups_are_removed(self):
        sanitized, count = sanitize_public_prose(
            "The caller survives (`ev_a`, ev_b). Verify it [res_a].",
            {"ev_a", "ev_b", "res_a"},
        )
        self.assertEqual(count, 3)
        self.assertEqual(sanitized, "The caller survives. Verify it.")

    def test_ci_identity_is_humanized_with_the_existing_safe_label_pipe(self):
        identity = "ci:check_run:89663657479"
        unsafe_name = "Build\r\n@release `candidate` " + ("x" * 140)
        review = {
            "decision": {
                "public_sentence": (
                    f"See `{identity}`, then inspect ({identity}); "
                    "run 89663657479."
                ),
                "reasons": [],
            },
            "owner_action": [],
            "findings": [{"id": "F1", "evidence_refs": [identity]}],
            "material_unknowns": [],
            "evidence_scope": [],
            "diagram": None,
        }
        sanitized, count = sanitize_review_for_publication(
            review,
            context_meta={
                "ci_snapshot": {
                    "checks": [
                        {
                            "identity": "check_run:89663657479",
                            "name": unsafe_name,
                        }
                    ]
                }
            },
        )
        sentence = sanitized["decision"]["public_sentence"]

        self.assertEqual(count, 3)
        self.assertNotIn(identity, sentence)
        self.assertNotIn("89663657479", sentence)
        self.assertNotIn("\r", sentence)
        self.assertNotIn("\n", sentence)
        self.assertNotIn("@release", sentence)
        self.assertIn("@\u200brelease", sentence)
        self.assertIn("the `Build @\u200brelease 'candidate'", sentence)
        self.assertNotIn("`the `", sentence)
        self.assertIn("...", sentence)

    def test_public_url_survives_derived_run_id_alias_substitution(self):
        """A public link is atomic: never rewrite one of its segments."""

        identity = "ci:check_run:90069459465"
        context_meta = {
            "ci_snapshot": {
                "checks": [
                    {"identity": "check_run:90069459465", "name": "ci"}
                ]
            }
        }
        review = {
            "decision": {"public_sentence": "Merge readiness needs work.", "reasons": []},
            "owner_action": [
                {
                    "text": (
                        "Review the run log at "
                        "https://github.com/owner/repo/actions/runs/302937/job/90069459465"
                        " to identify the failing step."
                    ),
                    "resolves": ["U1"],
                }
            ],
            "findings": [{"id": "F1", "evidence_refs": [identity]}],
            "material_unknowns": [
                {
                    "id": "U1",
                    "claim": "The failing step is unknown.",
                    "how_to_check": "Open run 90069459465 and read the log.",
                    "evidence_refs": [],
                }
            ],
            "evidence_scope": [],
            "diagram": None,
        }
        sanitized, _count = sanitize_review_for_publication(
            review, context_meta=context_meta
        )
        action = sanitized["owner_action"][0]["text"]
        self.assertIn(
            "https://github.com/owner/repo/actions/runs/302937/job/90069459465",
            action,
        )
        # The same run id outside a URL is still machine vocabulary.
        self.assertNotIn(
            "90069459465", sanitized["material_unknowns"][0]["how_to_check"]
        )
        self.assertIn(
            "the cited check", sanitized["material_unknowns"][0]["how_to_check"]
        )

    def test_safe_url_preserves_queries_parentheses_and_unrelated_numbers(self):
        identity = "ci:check_run:90069459465"
        url = (
            "https://github.com/owner/repo/actions/runs/302937/"
            "job/90069459465?view=(summary)&attempt=2"
        )
        sanitized, count = sanitize_public_prose(
            f"Inspect [{identity}] at {url}; retry 302937 if needed.",
            {identity},
            replacements={
                identity: "the `Build` check",
                "90069459465": "the cited check",
            },
        )
        self.assertEqual(count, 1)
        self.assertIn(url, sanitized)
        self.assertIn("retry 302937", sanitized)
        self.assertNotIn(identity, sanitized)

    def test_bare_url_with_multiple_private_ids_is_removed_as_one_atom(self):
        sanitized, count = sanitize_public_prose(
            (
                "Details: https://internal.example/run_(ev_a)"
                "?resolution=res_b&attempt=302937, then retry 42."
            ),
            {"ev_a", "res_b"},
        )
        self.assertEqual(count, 2)
        self.assertEqual(
            sanitized,
            "Details: the cited evidence, then retry 42.",
        )

    def test_markdown_and_autolink_private_urls_drop_their_whole_wrappers(self):
        sanitized, count = sanitize_public_prose(
            (
                "Use [the run](https://x.test/job_(ev_a)?resolution=res_a) "
                "or <https://x.test/q_q1?obligation=obl_1>."
            ),
            {"ev_a", "res_a", "q_q1", "obl_1"},
        )
        self.assertEqual(count, 4)
        self.assertEqual(sanitized, "Use the run or the cited evidence.")
        self.assertNotIn("https://", sanitized)
        self.assertNotIn("](", sanitized)
        self.assertNotIn("<the cited evidence>", sanitized)

    def test_escaped_markdown_parenthesis_does_not_split_private_url_atom(self):
        sanitized, count = sanitize_public_prose(
            (
                r"Use [the run](https://x.test/job_\(old\)/ev_a"
                r"?resolution=res_a) now."
            ),
            {"ev_a", "res_a"},
        )
        self.assertEqual(count, 2)
        self.assertEqual(sanitized, "Use the run now.")
        self.assertNotIn("https://", sanitized)
        self.assertNotIn("](", sanitized)

    def test_safe_escaped_markdown_parenthesis_is_byte_faithful(self):
        source = r"Use [the run](https://x.test/job_\(old\)?attempt=2) now."
        sanitized, count = sanitize_public_prose(source, {"ev_other"})
        self.assertEqual(count, 0)
        self.assertEqual(sanitized, source)

    def test_safe_markdown_destination_is_preserved_while_label_is_sanitized(self):
        url = "https://docs.example/reference_(v3)?section=42"
        sanitized, count = sanitize_public_prose(
            f"[See ev_a]({url} \"public title\")",
            {"ev_a"},
        )
        self.assertEqual(count, 1)
        self.assertEqual(
            sanitized,
            f"[See the cited evidence]({url} \"public title\")",
        )

    def test_public_url_in_safe_markdown_label_is_not_partially_rewritten(self):
        label_url = "https://github.com/owner/repo/actions/jobs/90069459465"
        destination = "https://docs.example/build-log"
        sanitized, count = sanitize_public_prose(
            f"[{label_url}]({destination}) and run 90069459465",
            {"ci:check_run:90069459465"},
            replacements={"90069459465": "the cited check"},
        )
        self.assertEqual(count, 1)
        self.assertEqual(
            sanitized,
            f"[{label_url}]({destination}) and run the cited check",
        )

    def test_relative_markdown_and_mailto_destinations_are_atomic_too(self):
        sanitized, count = sanitize_public_prose(
            (
                "Read [the local note](../evidence/(ev_a).md), then contact "
                "<mailto:owner+res_a@example.test>."
            ),
            {"ev_a", "res_a"},
        )
        self.assertEqual(count, 2)
        self.assertEqual(
            sanitized,
            "Read the local note, then contact the cited evidence.",
        )
        self.assertNotIn("](", sanitized)
        self.assertNotIn("<mailto:", sanitized)

    def test_reference_definition_with_private_destination_is_removed_atomically(self):
        sanitized, count = sanitize_public_prose(
            "[docs][target]\n\n[target]: /logs/obl_0001",
            {"obl_0001"},
        )
        self.assertEqual(count, 1)
        self.assertEqual(sanitized, "[docs][target]")
        self.assertNotIn("/logs/", sanitized)
        self.assertNotIn("the cited evidence", sanitized)

    def test_plain_email_autolink_with_private_identity_is_neutralized(self):
        sanitized, count = sanitize_public_prose(
            "Contact <user+obl_0001@example.test> for details.",
            {"obl_0001"},
        )
        self.assertEqual(count, 1)
        self.assertEqual(
            sanitized,
            "Contact the cited evidence for details.",
        )
        self.assertNotIn("@example.test", sanitized)

    def test_review_copy_preserves_repository_code_but_omits_unsafe_suggestion(self):
        review = {
            "decision": {
                "public_sentence": "The failure is visible in ev_a.",
                "reasons": [{"text": "See ev_a.", "refs": ["F1"]}],
            },
            "owner_action": [
                {"text": "Verify detail_a.", "resolves": ["U1"]}
            ],
            "findings": [
                {
                    "id": "F1",
                    "headline": "Failure (`ev_a`)",
                    "comment": "The trace ev_a proves it.",
                    "code_snippet": "const ev_a = value;",
                    "suggested_code": "const ev_a = fixed;",
                    "evidence_refs": ["ev_a"],
                }
            ],
            "material_unknowns": [
                {
                    "id": "U1",
                    "claim": "The result in detail_a is unknown.",
                    "how_to_check": "Resolve detail_a.",
                    "evidence_refs": ["detail_a"],
                }
            ],
            "evidence_scope": ["ev_a"],
            "diagram": None,
        }
        sanitized, count = sanitize_review_for_publication(review)
        rendered_prose = str(sanitized)
        self.assertGreaterEqual(count, 6)
        self.assertNotIn("trace ev_a", rendered_prose)
        self.assertNotIn("in detail_a", rendered_prose)
        self.assertEqual(
            sanitized["findings"][0]["code_snippet"],
            "const ev_a = value;",
        )
        self.assertNotIn("suggested_code", sanitized["findings"][0])
        self.assertNotIn("suggestion_type", sanitized["findings"][0])

    def test_fenced_model_prose_is_sanitized_too(self):
        text = (
            "The private citation ev_a must disappear.\n\n"
            "```python\n"
            "ev_a = repository_value\n"
            "```\n\n"
            "The trailing ev_a citation also disappears."
        )

        sanitized, count = sanitize_public_prose(text, {"ev_a"})

        self.assertEqual(count, 3)
        self.assertNotIn("private citation ev_a", sanitized)
        self.assertNotIn("trailing ev_a", sanitized)
        self.assertNotIn("ev_a", sanitized)
        self.assertIn("```python", sanitized)

    def test_explicit_deep_identity_set_sanitizes_prose_and_mermaid(self):
        review = {
            "decision": {
                "public_sentence": "Final retained C1 and still needs U7.",
                "reasons": [],
            },
            "owner_action": [],
            "findings": [],
            "material_unknowns": [],
            "evidence_scope": [],
            "diagram": {
                "description": "C1 reaches the consumer.",
                "mermaid": "```mermaid\nsequenceDiagram\nA->>B: U7\n```",
                "finding_refs": [],
                "evidence_refs": [],
            },
        }

        sanitized, count = sanitize_review_for_publication(
            review,
            extra_private_identities=("C1", "U7"),
        )

        self.assertGreaterEqual(count, 3)
        self.assertNotIn("C1", sanitized["decision"]["public_sentence"])
        self.assertNotIn("U7", sanitized["decision"]["public_sentence"])
        self.assertIsNone(sanitized["diagram"])

    def test_mermaid_with_private_identity_is_omitted(self):
        review = {
            "decision": {"public_sentence": "Review complete.", "reasons": []},
            "owner_action": [],
            "findings": [{"id": "F1", "evidence_refs": ["ev_a"]}],
            "material_unknowns": [],
            "evidence_scope": ["ev_a"],
            "diagram": {
                "description": "Flow",
                "mermaid": "```mermaid\nsequenceDiagram\nA->>B: ev_a\n```",
                "finding_refs": ["F1"],
                "evidence_refs": ["ev_a"],
            },
        }
        sanitized, count = sanitize_review_for_publication(review)
        self.assertIsNone(sanitized["diagram"])
        self.assertEqual(count, 1)
    def test_unselected_catalog_and_question_ids_are_still_private(self):
        identities = collect_private_identities(
            {"findings": []},
            {
                "evidence_catalog": [
                    {
                        "id": "ev_unselected",
                        "question_id": "q_unselected",
                    }
                ],
                "evidence_ledger": {
                    "evidence_events": [
                        {
                            "id": "ev_gap",
                            "question_id": "q_gap",
                        }
                    ]
                },
            },
        )

        self.assertEqual(
            identities,
            {"ev_unselected", "q_unselected", "ev_gap", "q_gap"},
        )
        sanitized, count = sanitize_public_prose(
            "The private citations are ev_unselected, q_unselected, and ev_gap.",
            identities,
        )
        self.assertEqual(count, 3)
        self.assertNotIn("ev_unselected", sanitized)
        self.assertNotIn("q_unselected", sanitized)
        self.assertNotIn("ev_gap", sanitized)

    def test_fixed_presentation_never_publishes_private_identity_copy(self):
        pr_details = """# Pull Request

## File Changes
### src/app.py
```diff
@@ -1 +1 @@
-value = 1
+value = 2
```
"""
        context_meta = {
            "head_sha": "a" * 40,
            "analyzer_result": {
                "pr_type": "code",
                "risk_domains": [],
            },
            "evidence_catalog": [
                {
                    "id": "ev_private_changed_region",
                    "source_type": "diff",
                    "outcome": "hit",
                    "paths": ["src/app.py"],
                    "coverage_type": "changed_region",
                }
            ],
        }
        result = compile_presentation_v1(
            {
                "version": "presentation_v1",
                "decision": {
                    "verdict": "clear",
                    "confidence": "High",
                    "summary": (
                        "No review blocker found; "
                        "ev_private_changed_region proves the change."
                    ),
                    "owner_actions": [],
                },
                "findings": [],
                "material_unknowns": [],
                "confidence_checks": [],
                "diagram": None,
            },
            pr_details=pr_details,
            context_meta=context_meta,
        )

        self.assertEqual(result.status, "publishable")
        self.assertIsNotNone(result.review)
        self.assertNotIn(
            "ev_private_changed_region",
            result.review["pr_review_comment"],
        )


if __name__ == "__main__":
    unittest.main()
