"""Decoded source text must not be decoded again while packing PR evidence."""

from copy import deepcopy
import json
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from tests.unit.fakes import ensure_repo_root_on_path, set_default_env

ensure_repo_root_on_path()
set_default_env()

from lambdas.LlamaPReviewPipeline.pr_ingest import (
    _bounded_pr_markdown,
    _complete_line_excerpt,
    fetch_pr_details,
    json_to_markdown,
)


class PRIngestLiteralEscapeTests(unittest.TestCase):
    def test_json_decoded_patch_retains_literal_escapes_and_actual_line_endings(self):
        lines = [
            "@@ -1,1 +1,5 @@",
            r'-const newline = "\n";',
            r'+const newline = "\r\n";',
            r"+const pattern = /\r?\n/g;",
            r'+const path = "C:\\new\\repo";',
            r'+const config = {"literal":"\\n", "return":"\\r", "tab":"\t"};',
            '+const label = "café";',
        ]
        for separator in ("\n", "\r\n"):
            with self.subTest(separator=repr(separator)):
                patch_text = separator.join(lines) + separator
                payload = {"file_changes": [{"file_path": "src/example.js", "diff": patch_text}]}
                decoded = json.loads(json.dumps(payload))
                before = deepcopy(decoded)
                markdown = json_to_markdown(decoded)
                rendered = markdown.split("```diff\n", 1)[1].split("\n```\n", 1)[0]
                self.assertEqual(rendered.encode("utf-8"), patch_text.encode("utf-8"))
                self.assertEqual(decoded, before)
                self.assertIn("## File Changes\n### src/example.js\n", markdown)

    def test_nested_issue_and_content_values_preserve_source_escapes(self):
        source = "first actual line\n" + r'pattern /\r?\n/; literal "\\n"; path C:\new\repo'
        markdown = json_to_markdown(
            {
                "pr_metadata": {"number": 1, "title": "Keep escapes", "author": source},
                "related_issues": [
                    {
                        "issue_number": 2,
                        "issue_content": "This is a Github Issue related to repo\n\n" + source,
                    }
                ],
                "dependency_changes": [{"file_path": "package.json", "content": source}],
                "config_changes": [{"file_path": "config.ini", "content": source}],
                "interactions": [{"author": "human", "body": source}],
            }
        )
        self.assertEqual(markdown.count(source), 5)
        self.assertIn("## Metadata\n- **number**: 1\n", markdown)
        self.assertIn("## Related Issues\n### Issue #2\n" + source + "\n\n", markdown)
        self.assertIn("### package.json\n```\n" + source + "\n```\n", markdown)
        self.assertNotIn("This is a Github Issue related to repo", markdown)

    def test_complete_line_excerpt_uses_only_real_newline_boundaries(self):
        source = r"+const pattern = /\r?\n/;"
        self.assertEqual(_complete_line_excerpt(source, len(source)), (source, True))
        self.assertEqual(_complete_line_excerpt(source, len(source) - 1), ("", False))
        prefix = "@@ -1 +1 @@\n" + source
        truncated = prefix + '\n+"' + "x" * 200 + '"'
        self.assertEqual(_complete_line_excerpt(truncated, len(prefix) + 30), (prefix, False))
        self.assertEqual(_complete_line_excerpt(truncated, 0), ("", False))

    def test_compacted_pr_details_preserve_literal_escapes_and_complete_source_lines(self):
        # The same JavaScript/regex forms occur in real release-tool patches.
        # Keep this fixture synthetic; complete captured patches remain private.
        source_line = r'+const pattern = /\[\[package\]\]\r?\nname = "example"\r?\n/;'
        patch_text = "@@ -1 +1,100 @@\n" + "\n".join([source_line] * 100)
        content = {
            "pr_metadata": {"number": 1, "title": "Keep literal escapes"},
            "file_changes": [{"file_path": "src/version.mjs", "diff": patch_text}],
        }
        before = deepcopy(content)
        markdown, meta = _bounded_pr_markdown(content, max_chars=1200, compact_above_chars=1200)
        excerpt = markdown.split("```diff\n", 1)[1].split("\n```", 1)[0]
        self.assertTrue(meta["pr_details_compacted"])
        self.assertLessEqual(len(markdown), 1200)
        self.assertIn("**Diff coverage:** partial\n", markdown)
        self.assertIn(source_line, excerpt)
        self.assertTrue(patch_text.startswith(excerpt + "\n"))
        self.assertEqual(excerpt.splitlines()[1:], [source_line] * (len(excerpt.splitlines()) - 1))
        self.assertEqual(content, before)

    def test_fetch_to_full_and_compacted_details_keeps_one_exact_source(self):
        source_line = r"+const pattern = /\r?\n/;"
        patch_text = "@@ -1 +1,100 @@\n" + "\n".join([source_line] * 100)
        runtime = MagicMock()
        runtime.get_pr_content.return_value = {
            "pr_metadata": {"number": 1, "title": "Keep source escapes"},
            "file_changes": [{"file_path": "src/version.mjs", "diff": patch_text}],
        }
        runtime.get_repository.return_value = SimpleNamespace(repo=SimpleNamespace(description=""))
        for limit in (100_000, 1200):
            with self.subTest(limit=limit), patch(
                "lambdas.LlamaPReviewPipeline.pr_ingest.config.LARGE_PR_MAX_CHARS", limit
            ):
                content, markdown = fetch_pr_details(runtime, "owner/repo", 1, max_chars=limit)
                self.assertEqual(content["file_changes"][0]["diff"], patch_text)
                excerpt = markdown.split("```diff\n", 1)[1].split("\n```", 1)[0]
                self.assertTrue(patch_text.startswith(excerpt))
                self.assertIn(source_line, excerpt)
                self.assertLessEqual(len(markdown), limit)


if __name__ == "__main__":
    unittest.main()
