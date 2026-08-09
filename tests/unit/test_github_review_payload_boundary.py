"""Strict external-payload contracts for GitHub PR review comments."""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from tests.unit.fakes import ensure_repo_root_on_path, set_default_env

ensure_repo_root_on_path()
set_default_env()

from lambdas.LlamaPReviewPipeline.review.publish import (
    build_diff_index_and_maps,
    parse_diff,
    prepare_github_review_request,
    prepare_main_comment_publication,
    prepare_review_publication,
    resolve_inline_placements,
)
from lambdas.LlamaPReviewPipeline.review.github_publication_surface import (
    _dispatch_exact_review,
)
from lambdas.LlamaPReviewPipeline.errors import (
    HeadSuperseded,
    PublicationIdentityUnavailable,
    PublicationIntegrityFailure,
)


SINGLE_LINE_PATCH = """@@ -1,2 +1,3 @@
 def configure():
+    timeout = 900
     return True
"""

MULTILINE_PATCH = """@@ -1,2 +1,4 @@
 def configure():
+    timeout = 900
+    retries = 2
     return True
"""

BLANK_LINE_MULTILINE_PATCH = """@@ -0,0 +1,4 @@
+def dispatch():
+    prepare()
+
+    send()
"""

DIRECT_AND_CONCEPTUAL_PATCH = """@@ -1,2 +1,4 @@
 def configure():
+    timeout = max_timeout(900)
+    retries = 2
     return True
"""

CONTEXTUAL_PATCH = """@@ -3,2 +3,2 @@
 def run():
-    old()
+    new()
"""


def _diff_maps(path: str, patch_text: str):
    _index, old_map, new_map = build_diff_index_and_maps(patch_text)
    return {
        path: {
            "hunks": parse_diff(patch_text),
            "old": old_map,
            "new": new_map,
        }
    }


class _CapturePull:
    def __init__(
        self,
        *,
        error=None,
        returned_review_id=1,
        returned_commit_id="abc123",
        returned_inline_comment_ids=None,
    ):
        self.calls = []
        self.error = error
        self.returned_review_id = returned_review_id
        self.returned_commit_id = returned_commit_id
        self.returned_inline_comment_ids = returned_inline_comment_ids

    def create_review(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        result = SimpleNamespace()
        if self.returned_review_id is not None:
            result.id = self.returned_review_id
        if self.returned_commit_id is not None:
            result.commit_id = self.returned_commit_id
        if self.returned_inline_comment_ids is not None:
            result.raw_data = {
                "comments": [
                    {"id": comment_id}
                    for comment_id in self.returned_inline_comment_ids
                ]
            }
        return result


class _CaptureRepo:
    def __init__(
        self,
        *,
        error=None,
        resolved_sha="abc123",
        returned_review_id=1,
        returned_commit_id="abc123",
        returned_inline_comment_ids=None,
    ):
        self.pull = _CapturePull(
            error=error,
            returned_review_id=returned_review_id,
            returned_commit_id=returned_commit_id,
            returned_inline_comment_ids=returned_inline_comment_ids,
        )
        self.commit = SimpleNamespace(sha=resolved_sha)
        self.commit_calls = []

    def get_pull(self, _number):
        return self.pull

    def get_commit(self, *, sha):
        self.commit_calls.append(sha)
        return self.commit


class GitHubReviewPayloadBoundaryTest(unittest.TestCase):
    def test_single_line_shape_keeps_only_supported_request_fields(self):
        """Replay the failed single-line shape without contacting GitHub."""

        placement = {
            "path": "src/config.py",
            "body": "Use the bounded timeout.",
            "line": 2,
            "side": "RIGHT",
            "layer": 1,
            "evidence_ids": ["E1"],
            "diagnostic": {"anchor": "exact"},
        }
        prepared = prepare_github_review_request(
            "Summary",
            [placement],
            head_sha="abc123",
        )

        self.assertEqual(
            prepared.request_payload(),
            {
                "head_sha": "abc123",
                "body": "Summary",
                "event": "COMMENT",
                "comments": [
                    {
                        "path": "src/config.py",
                        "body": "Use the bounded timeout.",
                        "line": 2,
                        "side": "RIGHT",
                    }
                ],
            },
        )
        self.assertEqual(placement["layer"], 1)
        self.assertIn("diagnostic", placement)

    def test_multiline_shape_keeps_only_supported_request_fields(self):
        """Replay the failed multiline shape without contacting GitHub."""

        placement = {
            "path": "src/service.py",
            "body": "Replace this range atomically.",
            "start_line": 8,
            "start_side": "RIGHT",
            "line": 10,
            "side": "RIGHT",
            "layer": "Layer 2",
            "unknown_internal_field": "must not cross the boundary",
        }
        prepared = prepare_github_review_request(
            "Summary",
            [placement],
            head_sha="abc123",
        )

        self.assertEqual(
            prepared.request_payload()["comments"],
            [
                {
                    "path": "src/service.py",
                    "body": "Replace this range atomically.",
                    "line": 10,
                    "side": "RIGHT",
                    "start_line": 8,
                    "start_side": "RIGHT",
                }
            ],
        )

    def test_resolver_builds_single_and_multiline_internal_placements(self):
        single = resolve_inline_placements(
            {
                "inline_comments": [
                    {
                        "file_path": "src/config.py",
                        "code_snippet": "timeout = 900",
                        "comment": "Keep the timeout bounded.",
                        "priority": "P1",
                        "confidence": "High",
                    }
                ]
            },
            _diff_maps("src/config.py", SINGLE_LINE_PATCH),
        )["inline_comments"]
        multiline = resolve_inline_placements(
            {
                "inline_comments": [
                    {
                        "file_path": "src/config.py",
                        "code_snippet": "timeout = 900\nretries = 2",
                        "comment": "Keep the retry policy together.",
                        "priority": "P1",
                        "confidence": "High",
                    }
                ]
            },
            _diff_maps("src/config.py", MULTILINE_PATCH),
        )["inline_comments"]

        self.assertEqual(
            {key: single[0][key] for key in ("path", "line", "side", "layer")},
            {
                "path": "src/config.py",
                "line": 2,
                "side": "RIGHT",
                "layer": 1,
            },
        )
        self.assertEqual(multiline[0]["start_line"], 2)
        self.assertEqual(multiline[0]["start_side"], "RIGHT")
        self.assertEqual(multiline[0]["line"], 3)
        self.assertEqual(multiline[0]["layer"], 1)

    def test_multiline_range_preserves_internal_blank_physical_lines(self):
        resolved = resolve_inline_placements(
            {
                "inline_comments": [
                    {
                        "file_path": "src/dispatch.py",
                        "code_snippet": (
                            "def dispatch():\n"
                            "    prepare()\n"
                            "\n"
                            "    send()"
                        ),
                        "comment": "Keep this operation ordered.",
                        "priority": "P1",
                        "confidence": "High",
                    }
                ]
            },
            _diff_maps(
                "src/dispatch.py",
                BLANK_LINE_MULTILINE_PATCH,
            ),
        )["inline_comments"]

        self.assertEqual(len(resolved), 1)
        self.assertEqual(resolved[0]["start_line"], 1)
        self.assertEqual(resolved[0]["line"], 4)

    def test_contextual_duplicates_are_deduplicated_before_boundary_sanitization(self):
        finding = {
            "file_path": "src/service.py",
            "code_snippet": "def helper():\n    return 1",
            "comment": "Keep this helper aligned with the changed caller.",
            "priority": "P2",
            "confidence": "Medium",
        }
        resolved = resolve_inline_placements(
            {"inline_comments": [finding, dict(finding)]},
            _diff_maps("src/service.py", CONTEXTUAL_PATCH),
            file_contents={
                "src/service.py": (
                    "def helper():\n"
                    "    return 1\n"
                    "def run():\n"
                    "    new()\n"
                )
            },
        )

        self.assertEqual(len(resolved["inline_comments"]), 1)
        self.assertEqual(resolved["inline_comments"][0]["layer"], "Layer 2")
        self.assertIn(
            "**[Contextual Comment]**",
            resolved["inline_comments"][0]["body"],
        )

        prepared = prepare_github_review_request(
            "Summary",
            resolved["inline_comments"],
            head_sha="abc123",
        )
        sent = prepared.request_payload()["comments"]

        self.assertEqual(len(sent), 1)
        self.assertEqual(set(sent[0]), {"path", "body", "line", "side"})
        self.assertNotIn("layer", sent[0])

    def test_inline_rendering_uses_evidence_note_not_retired_reference_tokens(self):
        base = {
            "file_path": "src/config.py",
            "code_snippet": "timeout = 900",
            "comment": "Keep the timeout bounded.",
            "priority": "P1",
            "confidence": "High",
        }
        retired = resolve_inline_placements(
            {
                "inline_comments": [
                    {
                        **base,
                        "evidence_references": ["symbol:configure"],
                    }
                ]
            },
            _diff_maps("src/config.py", SINGLE_LINE_PATCH),
        )["inline_comments"][0]["body"]
        current = resolve_inline_placements(
            {
                "inline_comments": [
                    {
                        **base,
                        "evidence_note": "Exact-head diff and CI agree",
                    }
                ]
            },
            _diff_maps("src/config.py", SINGLE_LINE_PATCH),
        )["inline_comments"][0]["body"]

        self.assertNotIn("symbol:configure", retired)
        self.assertNotIn("_Evidence:", retired)
        self.assertIn(
            "Evidence: Exact-head diff and CI agree.",
            current,
        )

    def test_live_publish_preserves_direct_and_conceptual_rendering_but_sends_only_supported_keys(self):
        final_json = {
            "pr_review_comment": "Two focused suggestions.",
            "review_publishable": True,
            "review_publication_safe": True,
            "review_generation_status": "complete",
            "review_fallback_used": False,
            "inline_comments": [
                {
                    "file_path": "src/config.py",
                    "code_snippet": "timeout = max_timeout(900)",
                    "comment": "Use the established timeout.",
                    "priority": "P1",
                    "confidence": "High",
                    "suggested_code": "timeout = max_timeout(600)",
                    "suggestion_type": "DIRECT_REPLACEMENT",
                },
                {
                    "file_path": "src/config.py",
                    "code_snippet": "retries = 2",
                    "comment": "Centralize the retry policy.",
                    "priority": "P2",
                    "confidence": "Medium",
                    "suggested_code": "retry_policy = load_policy()",
                    "suggestion_type": "CONCEPTUAL_ADVICE",
                },
            ],
        }
        repo = _CaptureRepo(
            returned_inline_comment_ids=[101, 102],
        )

        prepared = prepare_review_publication(
            final_json,
            head_sha="abc123",
            diff_maps=_diff_maps("src/config.py", DIRECT_AND_CONCEPTUAL_PATCH),
        )
        effect = _dispatch_exact_review(repo, 1, prepared)
        artifact = prepared.artifact

        self.assertEqual(len(artifact["inline_comments"]), 2)
        self.assertTrue(
            all("layer" in comment for comment in artifact["inline_comments"])
        )
        sent = repo.pull.calls[0]["comments"]
        self.assertEqual(len(sent), 2)
        self.assertTrue(
            all(
                set(comment).issubset(
                    {
                        "path",
                        "body",
                        "line",
                        "side",
                        "start_line",
                        "start_side",
                    }
                )
                for comment in sent
            )
        )
        rendered = "\n".join(comment["body"] for comment in sent)
        self.assertIn(
            "```suggestion\ntimeout = max_timeout(600)\n```",
            rendered,
        )
        self.assertIn(
            "**Conceptual guidance (not a committable GitHub suggestion):**",
            rendered,
        )
        self.assertIn("retry_policy = load_policy()", rendered)
        self.assertEqual(repo.commit_calls, ["abc123"])
        self.assertEqual(len(repo.pull.calls), 1)
        self.assertIs(repo.pull.calls[0]["commit"], repo.commit)
        self.assertEqual(artifact["publication_status"], "not_published")
        self.assertIsNone(effect)

    def test_main_only_publish_is_pinned_to_one_exact_commit_and_one_write(self):
        repo = _CaptureRepo(returned_commit_id="abc123")

        prepared = prepare_main_comment_publication(
            "Summary",
            head_sha="abc123",
            review_mode="low",
        )
        effect = _dispatch_exact_review(repo, 1, prepared)
        artifact = prepared.artifact

        self.assertEqual(artifact["head_sha"], "abc123")
        self.assertEqual(repo.commit_calls, ["abc123"])
        self.assertEqual(len(repo.pull.calls), 1)
        self.assertEqual(repo.pull.calls[0]["comments"], [])
        self.assertIs(repo.pull.calls[0]["commit"], repo.commit)
        self.assertEqual(artifact["publication_status"], "not_published")
        self.assertIsNone(effect)

    def test_commit_resolution_or_review_identity_mismatch_fails_closed(self):
        prepared = prepare_main_comment_publication(
            "Summary",
            head_sha="abc123",
            review_mode="low",
        )
        resolution_mismatch = _CaptureRepo(resolved_sha="def456")
        with self.assertRaises(HeadSuperseded):
            _dispatch_exact_review(
                resolution_mismatch,
                1,
                prepared,
            )
        self.assertEqual(resolution_mismatch.pull.calls, [])

        response_mismatch = _CaptureRepo(returned_commit_id="def456")
        with self.assertRaises(PublicationIntegrityFailure):
            _dispatch_exact_review(
                response_mismatch,
                1,
                prepared,
            )
        self.assertEqual(len(response_mismatch.pull.calls), 1)

        for label, repo in (
            (
                "missing_review_id",
                _CaptureRepo(returned_review_id=None),
            ),
            (
                "missing_commit_id",
                _CaptureRepo(returned_commit_id=None),
            ),
        ):
            with self.subTest(label=label):
                with self.assertRaisesRegex(
                    PublicationIdentityUnavailable,
                    "without returning",
                ):
                    _dispatch_exact_review(
                        repo,
                        1,
                        prepared,
                    )
                self.assertEqual(len(repo.pull.calls), 1)

    def test_dry_run_keeps_internal_metadata_and_performs_zero_writes(self):
        repo = _CaptureRepo()

        artifact = prepare_review_publication(
            {
                "pr_review_comment": "Summary",
                "review_generation_status": "complete",
                "review_publishable": True,
                "review_publication_safe": True,
                "review_fallback_used": False,
                "inline_comments": [
                    {
                        "file_path": "src/config.py",
                        "code_snippet": "timeout = 900",
                        "comment": "Keep the timeout bounded.",
                        "priority": "P1",
                        "confidence": "High",
                    }
                ],
            },
            head_sha="abc123",
            diff_maps=_diff_maps("src/config.py", SINGLE_LINE_PATCH),
        ).artifact

        self.assertEqual(repo.pull.calls, [])
        self.assertEqual(artifact["inline_comments"][0]["layer"], 1)
        self.assertEqual(artifact["publication_status"], "not_published")
        self.assertNotIn("github_review_id", artifact)
        self.assertNotIn("github_review_commit_id", artifact)

    def test_publish_rejects_missing_or_false_publication_safety_without_write(self):
        for safety in (False, None):
            with self.subTest(safety=safety):
                repo = _CaptureRepo()
                review = {
                    "pr_review_comment": "Summary",
                    "review_generation_status": "complete",
                    "review_publishable": True,
                    "review_fallback_used": False,
                    "inline_comments": [],
                }
                if safety is not None:
                    review["review_publication_safe"] = safety

                with self.assertRaisesRegex(
                    ValueError,
                    "publishable, safe review",
                ):
                    prepare_review_publication(
                        review,
                        head_sha="abc123",
                        diff_maps={},
                    )

                self.assertEqual(repo.pull.calls, [])

    def test_dry_run_rejects_the_same_ineligible_review_as_live(self):
        repo = _CaptureRepo()

        with self.assertRaisesRegex(ValueError, "publishable, safe review"):
            prepare_review_publication(
                {
                    "pr_review_comment": "Summary",
                    "review_generation_status": "complete",
                    "review_publishable": True,
                    "review_fallback_used": False,
                    "inline_comments": [],
                },
                head_sha="abc123",
                diff_maps={},
            )

        self.assertEqual(repo.pull.calls, [])

    def test_github_422_propagates_after_payload_is_sanitized(self):
        error = RuntimeError("GitHub returned HTTP 422")
        error.status = 422
        repo = _CaptureRepo(error=error)

        prepared = prepare_github_review_request(
            "Summary",
            [
                {
                    "path": "src/config.py",
                    "body": "Comment",
                    "line": 2,
                    "side": "RIGHT",
                    "layer": 1,
                    "telemetry": "private",
                }
            ],
            head_sha="abc123",
        )
        with self.assertRaisesRegex(RuntimeError, "HTTP 422"):
            _dispatch_exact_review(repo, 1, prepared)

        self.assertEqual(
            repo.pull.calls[0]["comments"],
            [
                {
                    "path": "src/config.py",
                    "body": "Comment",
                    "line": 2,
                    "side": "RIGHT",
                }
            ],
        )


if __name__ == "__main__":
    unittest.main()
