import unittest

from lambdas.LlamaPReviewPipeline.pipeline_ci import with_current_ci_snapshot
from lambdas.LlamaPReviewPipeline.review.evidence_contract import (
    classify_changed_region_anchor,
)
from lambdas.LlamaPReviewPipeline.review.presentation_projection import (
    _structured_ci_public_state,
)
from lambdas.LlamaPReviewPipeline.review.render import render_v3_markdown
from lambdas.LlamaPReviewPipeline.review.rendering_safety import format_mermaid
from lambdas.LlamaPReviewPipeline.review.v3 import finding_evidence_capability


HEAD = "a" * 40


def pr_details(path: str, added_lines: list[str]) -> str:
    diff = "\n".join(f"+{line}" for line in added_lines)
    return (
        "# Pull Request #1\n\n## File Changes\n"
        f"### {path}\n```diff\n@@ -0,0 +1,{len(added_lines)} @@\n"
        f"{diff}\n```\n"
    )


def finding(path: str, snippet: str, requirement: str, refs: list[str]):
    return {
        "priority": "P1",
        "visibility": "headline",
        "claim_scope": "changed_region",
        "file_path": path,
        "code_snippet": snippet,
        "required_evidence_refs": refs,
        "supporting_evidence_refs": [],
        "representation_requirement": requirement,
    }


def context(path: str, *, full_file: str = ""):
    catalog = [
        {
            "id": f"path:{path}",
            "source_type": "diff",
            "outcome": "hit",
            "paths": [path],
            "coverage_type": "changed_region",
        }
    ]
    if full_file:
        catalog.append(
            {
                "id": "ev_full",
                "source_type": "pfr",
                "tool": "read_file",
                "outcome": "hit",
                "paths": [path],
                "coverage_type": full_file,
                "observed_state": "content_observed",
                "source_ref": f"pr_head:{HEAD}",
            }
        )
    return {"head_sha": HEAD, "evidence_catalog": catalog}


class ExactRepresentationInvariantTests(unittest.TestCase):
    def test_diff_decoration_is_not_source_for_front_matter(self):
        details = pr_details("post.md", ["---", "layout: post"])
        self.assertEqual(
            classify_changed_region_anchor("post.md", "---", details),
            "post_change",
        )
        self.assertEqual(
            classify_changed_region_anchor("post.md", " ---", details),
            "invalid",
        )

    def test_python_and_yaml_indentation_remain_exact(self):
        python = pr_details("app.py", ["def run():", "    return True"])
        yaml = pr_details("config.yml", ["root:", "  child: true"])
        self.assertEqual(
            classify_changed_region_anchor(
                "app.py", "    return True", python
            ),
            "post_change",
        )
        self.assertEqual(
            classify_changed_region_anchor("app.py", "return True", python),
            "invalid",
        )
        self.assertEqual(
            classify_changed_region_anchor(
                "config.yml", "  child: true", yaml
            ),
            "post_change",
        )
        self.assertEqual(
            classify_changed_region_anchor(
                "config.yml", "child: true", yaml
            ),
            "invalid",
        )

    def test_first_byte_blocker_requires_complete_exact_head_file(self):
        path = "post.md"
        details = pr_details(path, ["---", "layout: post"])
        diff_only = finding(
            path,
            "---",
            "exact_full_file",
            [f"path:{path}"],
        )
        self.assertFalse(
            finding_evidence_capability(
                diff_only,
                pr_details=details,
                context_meta=context(path),
            )["critical_supported"]
        )

        exact = finding(
            path,
            "---",
            "exact_full_file",
            [f"path:{path}", "ev_full"],
        )
        self.assertTrue(
            finding_evidence_capability(
                exact,
                pr_details=details,
                context_meta=context(path, full_file="full_file"),
            )["critical_supported"]
        )
        self.assertFalse(
            finding_evidence_capability(
                exact,
                pr_details=details,
                context_meta=context(path, full_file="file_slice"),
            )["critical_supported"]
        )

    def test_real_indentation_defect_remains_exact_postimage_evidence(self):
        path = "config.yml"
        details = pr_details(path, ["root:", " child: true"])
        exact = finding(
            path,
            " child: true",
            "exact_postimage",
            [f"path:{path}"],
        )
        self.assertTrue(
            finding_evidence_capability(
                exact,
                pr_details=details,
                context_meta=context(path),
            )["critical_supported"]
        )
        exact["code_snippet"] = "child: true"
        self.assertFalse(
            finding_evidence_capability(
                exact,
                pr_details=details,
                context_meta=context(path),
            )["critical_supported"]
        )

    def test_unobserved_or_truncated_file_cannot_support_full_file_claim(self):
        path = "header.py"
        details = pr_details(path, ["#!/usr/bin/env python3"])
        exact = finding(
            path,
            "#!/usr/bin/env python3",
            "exact_full_file",
            [f"path:{path}", "ev_full"],
        )
        for observed_state in ("unavailable", "truncated"):
            meta = context(path)
            meta["evidence_catalog"].append(
                {
                    "id": "ev_full",
                    "source_type": "pfr",
                    "tool": "read_file",
                    "outcome": "partial",
                    "paths": [path],
                    "coverage_type": "full_file",
                    "observed_state": observed_state,
                    "source_ref": f"pr_head:{HEAD}",
                }
            )
            with self.subTest(observed_state=observed_state):
                self.assertFalse(
                    finding_evidence_capability(
                        exact,
                        pr_details=details,
                        context_meta=meta,
                    )["critical_supported"]
                )


class StructuredCIPresentationTests(unittest.TestCase):
    def _review(self, posture: str, counts: dict, retrieval: str = "ok"):
        return {
            "visible_verdict": "clear",
            "decision": {
                "public_sentence": (
                    "No review blocker found. Every completed gate passed."
                )
            },
            "owner_action": [],
            "findings": [],
            "material_unknowns": [],
            "evidence_scope": [],
            "diagram": None,
            "rendering_plan": {
                "ci_public_state": {
                    "posture": posture,
                    "retrieval_outcome": retrieval,
                    "counts": counts,
                }
            },
        }

    def test_red_or_partial_ci_replaces_conflicting_clear_prose(self):
        rendered = render_v3_markdown(
            self._review(
                "unresolved",
                {"failure": 2, "pending": 1},
                retrieval="partial",
            )
        )
        self.assertNotIn("Every completed gate passed", rendered)
        self.assertIn("2 failed", rendered)
        self.assertIn("1 pending", rendered)
        self.assertIn("retrieval partial", rendered)
        self.assertIn("no CI-dependent merge-safety claim", rendered)

    def test_exact_unrelated_attribution_can_remain_clear(self):
        rendered = render_v3_markdown(
            self._review("unrelated_supported", {"failure": 1})
        )
        self.assertIn("1 failed", rendered)
        self.assertIn("outside this change", rendered)
        self.assertIn("Conditional code-review clear", rendered)

    def test_no_ci_surface_cannot_be_rendered_as_all_green(self):
        rendered = render_v3_markdown(
            self._review("not_observed", {}, retrieval="no_hit")
        )
        self.assertNotIn("Every completed gate passed", rendered)
        self.assertIn("reported no statuses or check runs", rendered)
        self.assertIn("Conditional code-review clear", rendered)

    def test_unrelated_attribution_requires_exact_diagnostic_or_source(self):
        snapshot = {
            "schema_version": 1,
            "has_ci": True,
            "retrieval_outcome": "ok",
            "checks": [
                {
                    "identity": "check_run:42",
                    "classification": "failure",
                }
            ],
        }
        checks = [
            {
                "ci_relevance": "unrelated",
                "evidence_refs": ["ci:check_run:42"],
            }
        ]
        self.assertEqual(
            _structured_ci_public_state(
                {"ci_snapshot": snapshot, "evidence_catalog": []}, checks
            )["posture"],
            "unresolved",
        )

        snapshot["checks"][0]["output"] = {
            "summary": "Dependency mirror is unavailable before this change runs."
        }
        self.assertEqual(
            _structured_ci_public_state(
                {"ci_snapshot": snapshot, "evidence_catalog": []}, checks
            )["posture"],
            "unrelated_supported",
        )

    def test_incomplete_snapshot_stays_unresolved_even_without_red(self):
        state = _structured_ci_public_state(
            {
                "ci_snapshot": {
                    "schema_version": 1,
                    "has_ci": True,
                    "retrieval_outcome": "partial",
                    "checks": [
                        {
                            "identity": "check_run:1",
                            "classification": "success",
                        }
                    ],
                }
            },
            [],
        )
        self.assertEqual(state["posture"], "unresolved")
        self.assertEqual(state["counts"]["success"], 1)

    def test_legacy_ci_markdown_is_replaced_by_one_structured_surface(self):
        details = (
            "# Pull Request #1\n\n## File Changes\n### app.py\n"
            "```diff\n+x = 1\n```\n\n## CI/CD Results\n\n"
            "### Check Runs\n- stale green\n\n## Interactions\n- note\n"
        )
        rendered = with_current_ci_snapshot(
            details,
            {
                "schema_version": 1,
                "aggregate_classification": "failure",
                "retrieval_outcome": "ok",
                "checks": [],
            },
        )
        self.assertNotIn("## CI/CD Results", rendered)
        self.assertNotIn("stale green", rendered)
        self.assertIn("## Interactions", rendered)
        self.assertEqual(rendered.count("<CURRENT_HEAD_CI_SNAPSHOT>"), 1)


class DeterministicPresentationCorrectionTests(unittest.TestCase):
    def test_empty_mermaid_groups_are_omitted_but_nonempty_group_remains(self):
        rendered = format_mermaid(
            "sequenceDiagram\n"
            "participant A\nparticipant B\n"
            "critical Label only\nend\n"
            "alt Useful path\nA->>B: request\nend"
        )
        self.assertNotIn("critical Label only", rendered)
        self.assertIn("alt Useful path", rendered)
        self.assertIn("A->>B: request", rendered)

    def test_duplicate_check_bullets_are_removed_without_merging_distinct(self):
        review = {
            "visible_verdict": "clear",
            "decision": {
                "public_sentence": "No review blocker found. Check: Passes."
            },
            "owner_action": [],
            "findings": [],
            "material_unknowns": [],
            "evidence_scope": [
                {"description": "Read the exact PR-head file."},
                {"description": "Read the exact PR-head file."},
                {"description": "Reviewed the changed caller."},
            ],
            "diagram": None,
        }
        rendered = render_v3_markdown(review)
        self.assertEqual(rendered.count("- Read the exact PR-head file."), 1)
        self.assertEqual(rendered.count("- Reviewed the changed caller."), 1)
        self.assertNotIn("Check: Passes.", rendered.split("<details>", 1)[0])


if __name__ == "__main__":
    unittest.main()
