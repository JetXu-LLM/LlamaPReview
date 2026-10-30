"""Bounded data-member reads keep exact peer declarations, not only headers."""

from __future__ import annotations

import json
import unittest

from tests.unit.fakes import ensure_repo_root_on_path, set_default_env

ensure_repo_root_on_path()
set_default_env()

from lambdas.LlamaPReviewPipeline.context_engine.assembler import assemble_context
from lambdas.LlamaPReviewPipeline.context_engine.code_extractor import CodeContextExtractor
from lambdas.LlamaPReviewPipeline.context_engine.repo_structure import RepoInventory
from lambdas.LlamaPReviewPipeline.context_engine.state import CollectionState
from lambdas.LlamaPReviewPipeline.context_engine.tools import ToolExecutor


HEAD = "a" * 40
LOCK = "frontend/package-lock.json"
FIRST = "node_modules/@sample/react"
SECOND = "node_modules/@sample/user-event"
PEER = "@sample/dom"


def _lock_text():
    # Minimal synthetic reproduction of the verified two-member failure:
    # required peers are 13/11 lines after the respective member headers. The
    # first also has unrelated optional peers; never infer required=optional.
    return json.dumps(
        {
            "packages": {
                "node_modules/@sample/react-extra": {"version": "decoy"},
                "node_modules/outer/node_modules/@sample/react": {"version": "nested-decoy"},
                FIRST: {
                    "version": "16.3.2",
                    "resolved": "https://example.test/react.tgz",
                    "integrity": "synthetic",
                    "dev": True,
                    "license": "MIT",
                    "dependencies": {"@sample/runtime": "^7.12.5"},
                    "engines": {"node": ">=18"},
                    "peerDependencies": {PEER: "^10.0.0", "@types/react": "^18 || ^19"},
                    "peerDependenciesMeta": {"@types/react": {"optional": True}},
                },
                SECOND: {
                    "version": "14.6.1",
                    "resolved": "https://example.test/user-event.tgz",
                    "integrity": "synthetic",
                    "dev": True,
                    "license": "MIT",
                    "engines": {"node": ">=12", "npm": ">=6"},
                    "peerDependencies": {PEER: ">=7.21.4"},
                },
            },
            "padding": "x" * 60_000,
        },
        indent=2,
    )


class _Runtime:
    def __init__(self, content):
        self.content = content
        self.calls = []

    def read_text_file_bounded(self, repo, path, *, sha=None, opt_in=None):
        self.calls.append((repo, path, sha, opt_in))
        size = len(self.content.encode("utf-8"))
        return {
            "content": self.content,
            "outcome": "success",
            "source_size_bytes": size,
            "bytes_read": size,
            "max_bytes": 2 * 1024 * 1024,
        }


def _executor(content):
    runtime = _Runtime(content)
    inventory = RepoInventory(
        repository="owner/repo",
        requested_sha=HEAD,
        status="complete",
        discoverable_files={LOCK},
        items=[{"type": "blob", "path": LOCK, "size": len(content.encode("utf-8"))}],
    )
    state = CollectionState(
        pr_details="synthetic dependency update",
        pr_content={"file_changes": []},
        repo_full_name="owner/repo",
        head_sha=HEAD,
        default_branch="main",
        runtime=runtime,
        repo_inventory=inventory,
        accessible_files={LOCK},
        max_read_calls=6,
    )
    return ToolExecutor(state), runtime


def _read(executor, symbols, question):
    args = {
        "path": LOCK,
        "mode": "content",
        "reason": "Inspect declared dependencies",
        "symbols": symbols,
    }
    question_id = executor.state.evidence_ledger.register_question(
        question=question, tool="read_file", args=args
    )
    return executor.execute(
        {"function": {"name": "read_file", "arguments": json.dumps(args)}}, question_id=question_id
    )


class JsonMemberEvidenceTests(unittest.TestCase):
    def test_fresh_read_retains_required_peers_and_exact_member_boundaries(self):
        content = _lock_text()
        executor, runtime = _executor(content)
        text = _read(executor, [FIRST, SECOND], "Inspect both retained peer declarations")
        self.assertIn(f'"{PEER}": "^10.0.0"', text)
        self.assertIn(f'"{PEER}": ">=7.21.4"', text)
        self.assertIn('"@types/react": {', text)
        self.assertNotIn('"decoy"', text)
        self.assertNotIn("nested-decoy", text)
        self.assertEqual(runtime.calls, [("owner/repo", LOCK, HEAD, "dependency_lock")])
        self.assertEqual(executor.state.read_calls, 1)
        for snippet in executor.state.collected_snippets:
            self.assertEqual(
                snippet["code"],
                "\n".join(content.splitlines()[snippet["start"] - 1 : snippet["end"]]),
            )
            self.assertLessEqual(snippet["end"] - snippet["start"] + 1, 200)
            self.assertTrue(snippet["exact_head_admitted"])
            self.assertIn(HEAD[:8], snippet["source"])
        event = executor.state.tool_events[-1]
        self.assertTrue(event["metadata"]["backend_attempted"])
        self.assertEqual(event["metadata"]["coverage_type"], "file_slice")
        self.assertFalse(event["metadata"]["backend_full_file_fetched"])
        self.assertEqual(event["source_ref"], f"pr_head:{HEAD}")
        context = assemble_context(executor.state)
        self.assertIn(f'"{PEER}": "^10.0.0"', context)
        self.assertIn(f'"{PEER}": ">=7.21.4"', context)

    def test_cached_read_preserves_peers_and_provenance_without_backend_purchase(self):
        executor, runtime = _executor(_lock_text())
        _read(executor, ["node_modules/@sample/react-extra"], "Inspect one prior slice")
        prior_id = executor.state.tool_events[-1]["evidence_event_id"]
        text = _read(executor, [FIRST, SECOND], "Inspect retained required peers")
        self.assertIn(f'"{PEER}": "^10.0.0"', text)
        self.assertIn(f'"{PEER}": ">=7.21.4"', text)
        self.assertEqual(len(runtime.calls), 1)
        self.assertEqual(executor.state.read_calls, 1)
        self.assertEqual(len(executor.state.tool_events), 2)
        event = executor.state.tool_events[-1]
        self.assertFalse(event["metadata"]["backend_attempted"])
        self.assertEqual(event["metadata"]["derived_from_event_id"], prior_id)
        self.assertEqual(event["metadata"]["coverage_type"], "file_slice")
        self.assertEqual(event["source_ref"], f"pr_head:{HEAD}")
        context = assemble_context(executor.state)
        self.assertIn(f'"{PEER}": "^10.0.0"', context)
        self.assertIn(f'"{PEER}": ">=7.21.4"', context)

    def test_exact_json_key_beats_prefix_and_nested_suffix_decoys(self):
        extractor = CodeContextExtractor()
        lines = _lock_text().splitlines()
        candidates = [i for i, line in enumerate(lines) if FIRST in line]
        self.assertEqual(len(candidates), 3)
        selected = extractor.pick_representative_line(candidates, lines, FIRST)
        self.assertEqual(json.loads(lines[selected].strip().split(":", 1)[0]), FIRST)
        nested = "node_modules/outer/node_modules/@sample/react"
        selected_nested = extractor.pick_representative_line(
            [i for i, line in enumerate(lines) if nested in line], lines, nested
        )
        block, _start, _end = extractor.extract_enclosing_block(
            "\n".join(lines), selected_nested, nested
        )
        self.assertIn("nested-decoy", block)
        self.assertNotIn("peerDependencies", block)

    def test_nonexact_json_key_does_not_expand_a_different_member(self):
        extractor = CodeContextExtractor()
        text = '"node_modules/@sample/react-extra": {\n  "peer": "other"\n}'
        self.assertEqual(
            extractor.extract_enclosing_block(text, 0, FIRST),
            (None, -1, -1),
        )
        exact = '"target": {\n  "peer": "required"\n}'
        for selector in ("target", '"target"', '"target":'):
            with self.subTest(selector=selector):
                self.assertEqual(
                    extractor.extract_enclosing_block(exact, 0, selector),
                    (exact, 1, 3),
                )

    def test_json_container_balancing_respects_strings_escapes_and_arrays(self):
        text = '"target": {\n  "note": "brace } and \\"quoted\\" [ text",\n  "values": [{"nested": "[}\\\\"}, 2]\n},\n"next": {"must_not_leak": true}'
        extractor = CodeContextExtractor()
        block, start, end = extractor.extract_enclosing_block(text, 0, "target")
        self.assertEqual((start, end), (1, 4))
        self.assertEqual(block, "\n".join(text.splitlines()[:4]))
        self.assertNotIn("must_not_leak", block)
        array = '"target": [\n  {"key": "}"},\n  [1, 2]\n],\n"next": {}'
        block, start, end = extractor.extract_enclosing_block(array, 0, "target")
        self.assertEqual((start, end), (1, 4))
        self.assertEqual(block, "\n".join(array.splitlines()[:4]))

    def test_invalid_truncated_or_oversize_json_keeps_existing_window_fallback(self):
        extractor = CodeContextExtractor()
        invalids = [
            '"target": {\n  "field": "unterminated\n}',
            '"target": {\n  "field": 1',
            '"target": {"field": NaN}',
            '"target": {"field": 1}, "sibling": {}',
            '"target": {\n'
            + "\n".join(f'  "field{i}": {i},' for i in range(199))
            + '\n  "last": true\n}',
        ]
        for text in invalids:
            with self.subTest(text=text[:60]):
                self.assertEqual(
                    extractor.extract_enclosing_block(text, 0, "target"), (None, -1, -1)
                )
                window, start, end = extractor.build_line_window(text, 0)
                self.assertEqual(window, "\n".join(text.splitlines()[start - 1 : end]))
                self.assertLessEqual(end - start + 1, 7)

    def test_json_member_obeys_the_existing_200_line_limit(self):
        extractor = CodeContextExtractor()
        text = (
            '"target": {\n'
            + "\n".join(f'  "field{i}": {i},' for i in range(197))
            + '\n  "last": true\n}'
        )
        self.assertEqual(len(text.splitlines()), 200)
        block, start, end = extractor.extract_enclosing_block(text, 0, "target")
        self.assertEqual((block, start, end), (text, 1, 200))
        text = text.replace('  "last": true', '  "extra": true,\n  "last": true')
        self.assertEqual(extractor.extract_enclosing_block(text, 0, "target"), (None, -1, -1))


if __name__ == "__main__":
    unittest.main()
