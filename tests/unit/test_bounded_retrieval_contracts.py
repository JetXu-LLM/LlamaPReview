import unittest
import json
from types import SimpleNamespace

from tests.unit.fakes import ensure_repo_root_on_path, set_default_env

ensure_repo_root_on_path()
set_default_env()

from lambdas.LlamaPReviewPipeline.context_engine.pfr import _fetch_health
from lambdas.LlamaPReviewPipeline.context_engine.assembler import _tool_trace_lines
from lambdas.LlamaPReviewPipeline.context_engine.state import CollectionState
from lambdas.LlamaPReviewPipeline.context_engine.tools import ToolExecutor


class _BoundedRuntime:
    def __init__(self, payloads):
        self.payloads = dict(payloads)
        self.calls = []

    def read_text_file_bounded(self, repo, path, *, sha=None, opt_in=None):
        self.calls.append((repo, path, sha, opt_in))
        return dict(self.payloads[path])


def _state(runtime, *paths):
    return CollectionState(
        pr_details="details",
        pr_content={"file_changes": []},
        repo_full_name="owner/repo",
        head_sha="abcdef123456",
        default_branch="main",
        runtime=runtime,
        accessible_files=set(paths),
        repo_inventory=SimpleNamespace(
            status="complete",
            record_read=lambda *_args, **_kwargs: None,
        ),
    )


class BoundedRetrievalContractsTest(unittest.TestCase):
    @staticmethod
    def _execute_read(executor, question_id, **args):
        return executor.execute(
            {
                "function": {
                    "name": "read_file",
                    "arguments": json.dumps({"reason": "verify contract", **args}),
                }
            },
            question_id=question_id,
        )

    def test_tool_trace_exposes_content_free_coverage_to_reconcile(self):
        state = _state(_BoundedRuntime({}))
        state.tool_events.append(
            {
                "tool": "read_file",
                "outcome": "hit",
                "hit_count": 1,
                "paths": ["package.json"],
                "metadata": {"coverage_type": "full_file"},
            }
        )

        rendered = "\n".join(_tool_trace_lines(state))
        self.assertIn("coverage=full_file", rendered)
        self.assertNotIn("repository text", rendered)

    def test_lock_and_ci_reads_use_only_their_explicit_opt_in(self):
        payloads = {
            "uv.lock": {
                "outcome": "success",
                "content": "package = []\n",
                "source_size_bytes": 13,
                "bytes_read": 13,
                "max_bytes": 2 * 1024 * 1024,
                "policy_class": "dependency_lock",
            },
            ".github/workflows/test.yml": {
                "outcome": "success",
                "content": "name: test\n",
                "source_size_bytes": 11,
                "bytes_read": 11,
                "max_bytes": 2 * 1024 * 1024,
                "policy_class": "ci_config",
            },
        }
        runtime = _BoundedRuntime(payloads)
        state = _state(runtime, *payloads)
        executor = ToolExecutor(state)

        self.assertEqual(executor.read_file({"path": "uv.lock"}).outcome, "hit")
        self.assertEqual(
            executor.read_file({"path": ".github/workflows/test.yml"}).outcome,
            "hit",
        )
        self.assertEqual(runtime.calls[0][3], "dependency_lock")
        self.assertEqual(runtime.calls[1][3], "ci_config")

    def test_small_success_is_full_file_evidence(self):
        content = "def run():\n    return 1\n"
        runtime = _BoundedRuntime(
            {
                "src/app.py": {
                    "outcome": "success",
                    "content": content,
                    "source_size_bytes": len(content.encode()),
                    "bytes_read": len(content.encode()),
                    "max_bytes": 2 * 1024 * 1024,
                }
            }
        )
        state = _state(runtime, "src/app.py")

        result = ToolExecutor(state).read_file({"path": "src/app.py"})

        self.assertEqual(result.outcome, "hit")
        self.assertEqual(result.metadata["coverage_type"], "full_file")
        self.assertEqual(state.collected_files["src/app.py"], content)

    def test_small_file_with_symbols_is_model_visible_slice_not_full_file(self):
        content = (
            "def first():\n"
            "    return 1\n\n"
            "def second():\n"
            "    return 2\n"
        )
        runtime = _BoundedRuntime(
            {
                "src/app.py": {
                    "outcome": "success",
                    "content": content,
                    "source_size_bytes": len(content.encode()),
                    "bytes_read": len(content.encode()),
                    "max_bytes": 2 * 1024 * 1024,
                }
            }
        )
        state = _state(runtime, "src/app.py")

        result = ToolExecutor(state).read_file(
            {"path": "src/app.py", "symbols": ["second"]}
        )

        self.assertEqual(result.outcome, "hit")
        self.assertEqual(result.metadata["coverage_type"], "file_slice")
        self.assertTrue(result.metadata["backend_full_file_fetched"])
        self.assertIn("second", state.collected_snippets[0]["code"])

    def test_truncated_small_payload_is_never_labeled_full_file(self):
        content = "def run():\n    return 1\n"
        runtime = _BoundedRuntime(
            {
                "src/app.py": {
                    "outcome": "success",
                    "content": content[:8],
                    "source_size_bytes": len(content.encode()),
                    "bytes_read": 8,
                    "max_bytes": 2 * 1024 * 1024,
                }
            }
        )
        state = _state(runtime, "src/app.py")

        result = ToolExecutor(state).read_file({"path": "src/app.py"})

        self.assertEqual(result.outcome, "no_hit")
        self.assertNotIn("coverage_type", result.metadata)
        self.assertNotIn("src/app.py", state.collected_files)

    def test_byte_cap_never_overrides_smaller_context_character_cap(self):
        content = "x" * 50_001
        payload = {
            "outcome": "success",
            "content": content,
            "source_size_bytes": len(content.encode()),
            "bytes_read": len(content.encode()),
            "max_bytes": 2 * 1024 * 1024,
        }

        without_symbol = _state(
            _BoundedRuntime({"src/boundary.txt": payload}),
            "src/boundary.txt",
        )
        no_hit = ToolExecutor(without_symbol).read_file(
            {"path": "src/boundary.txt"}
        )
        self.assertEqual(no_hit.outcome, "no_hit")
        self.assertNotIn("coverage_type", no_hit.metadata)
        self.assertNotIn("src/boundary.txt", without_symbol.collected_files)

        with_symbol = _state(
            _BoundedRuntime({"src/boundary.txt": payload}),
            "src/boundary.txt",
        )
        hit = ToolExecutor(with_symbol).read_file(
            {"path": "src/boundary.txt", "symbols": ["xxxx"]}
        )
        self.assertEqual(hit.outcome, "hit")
        self.assertEqual(hit.metadata["coverage_type"], "file_slice")

    def test_large_file_requires_a_real_symbol_hit_and_can_find_tail_symbol(self):
        prefix = "padding line\n" * 5000
        content = prefix + "def tail_symbol():\n    return 7\n"
        payload = {
            "outcome": "success",
            "content": content,
            "source_size_bytes": len(content.encode()),
            "bytes_read": len(content.encode()),
            "max_bytes": 2 * 1024 * 1024,
        }

        with_symbol = _state(_BoundedRuntime({"src/large.py": payload}), "src/large.py")
        hit = ToolExecutor(with_symbol).read_file(
            {"path": "src/large.py", "symbols": ["tail_symbol"]}
        )
        self.assertEqual(hit.outcome, "hit")
        self.assertEqual(hit.metadata["coverage_type"], "file_slice")
        self.assertIn("tail_symbol", hit.text)
        self.assertEqual(
            with_symbol.collected_files["src/large.py"],
            "[bounded symbol slices retained]",
        )

        without_symbol = _state(
            _BoundedRuntime({"src/large.py": payload}), "src/large.py"
        )
        no_hit = ToolExecutor(without_symbol).read_file({"path": "src/large.py"})
        self.assertEqual(no_hit.outcome, "no_hit")
        self.assertEqual(without_symbol.read_outcomes["src/large.py"], "symbols_not_provided")
        self.assertNotIn("src/large.py", without_symbol.collected_files)

    def test_typed_terminal_outcomes_remain_content_free_and_failed(self):
        for outcome in (
            "excluded_by_policy",
            "not_found",
            "oversize",
            "binary_or_non_utf8",
            "directory",
            "error",
        ):
            with self.subTest(outcome=outcome):
                runtime = _BoundedRuntime(
                    {
                        "src/value": {
                            "outcome": outcome,
                            "content": None,
                            "source_size_bytes": 99,
                            "bytes_read": 0,
                            "max_bytes": 2 * 1024 * 1024,
                            "error_type": "fixture",
                        }
                    }
                )
                state = _state(runtime, "src/value")
                result = ToolExecutor(state).read_file({"path": "src/value"})
                self.assertEqual(result.outcome, outcome)
                self.assertNotIn("repository text", result.text)
                self.assertEqual(state.read_outcomes["src/value"], outcome)

    def test_full_file_evidence_is_derived_for_a_new_question_without_backend_read(self):
        content = "def run():\n    return 1\n"
        runtime = _BoundedRuntime(
            {
                "src/app.py": {
                    "outcome": "success",
                    "content": content,
                    "source_size_bytes": len(content.encode()),
                    "bytes_read": len(content.encode()),
                    "max_bytes": 2 * 1024 * 1024,
                }
            }
        )
        state = _state(runtime, "src/app.py")
        first_q = state.evidence_ledger.register_question(
            question="Inspect implementation.",
            tool="read_file",
            args={"path": "src/app.py"},
        )
        second_q = state.evidence_ledger.register_question(
            question="Verify caller-facing contract.",
            tool="read_file",
            args={"path": "src/app.py"},
        )
        executor = ToolExecutor(state)

        self._execute_read(executor, first_q, path="src/app.py")
        self._execute_read(executor, second_q, path="src/app.py")

        self.assertEqual(len(runtime.calls), 1)
        first, second = state.tool_events
        self.assertTrue(first["metadata"]["backend_attempted"])
        self.assertFalse(second["metadata"]["backend_attempted"])
        self.assertEqual(second["metadata"]["coverage_type"], "full_file")
        self.assertEqual(
            second["metadata"]["derived_from_event_id"],
            first["evidence_event_id"],
        )
        self.assertNotEqual(second["evidence_event_id"], first["evidence_event_id"])
        self.assertEqual(
            state.evidence_ledger.events[second["evidence_event_id"]]["question_id"],
            second_q,
        )

    def test_cached_source_derives_only_a_real_new_literal_slice(self):
        content = ("padding\n" * 8_000) + "def tail_symbol():\n    return 7\n"
        payload = {
            "outcome": "success",
            "content": content,
            "source_size_bytes": len(content.encode()),
            "bytes_read": len(content.encode()),
            "max_bytes": 2 * 1024 * 1024,
        }
        runtime = _BoundedRuntime({"src/large.py": payload})
        state = _state(runtime, "src/large.py")
        executor = ToolExecutor(state)
        question_ids = [
            state.evidence_ledger.register_question(
                question=question,
                tool="read_file",
                args={"path": "src/large.py", "symbols": [symbol]},
            )
            for question, symbol in (
                ("Inspect padding.", "padding"),
                ("Inspect tail.", "tail_symbol"),
                ("Inspect missing literal.", "never_present_literal"),
            )
        ]

        self._execute_read(
            executor, question_ids[0], path="src/large.py", symbols=["padding"]
        )
        self._execute_read(
            executor, question_ids[1], path="src/large.py", symbols=["tail_symbol"]
        )
        self._execute_read(
            executor,
            question_ids[2],
            path="src/large.py",
            symbols=["never_present_literal"],
        )

        self.assertEqual(len(runtime.calls), 1)
        self.assertEqual(state.tool_events[1]["outcome"], "hit")
        self.assertEqual(state.tool_events[1]["metadata"]["coverage_type"], "file_slice")
        self.assertFalse(state.tool_events[1]["metadata"]["backend_attempted"])
        self.assertEqual(state.tool_events[2]["outcome"], "no_hit")
        self.assertFalse(state.tool_events[2]["metadata"]["backend_attempted"])
        self.assertEqual(state.tool_events[2]["metadata"].get("coverage_type"), "")
        self.assertNotIn("never_present_literal", state.tool_events[2]["paths"])

    def test_cached_symbol_then_full_read_exposes_real_full_content(self):
        content = (
            "def first():\n"
            "    return 1\n\n"
            "def second():\n"
            "    return 2\n"
        )
        runtime = _BoundedRuntime(
            {
                "src/app.py": {
                    "outcome": "success",
                    "content": content,
                    "source_size_bytes": len(content.encode()),
                    "bytes_read": len(content.encode()),
                    "max_bytes": 2 * 1024 * 1024,
                }
            }
        )
        state = _state(runtime, "src/app.py")
        executor = ToolExecutor(state)
        slice_question = state.evidence_ledger.register_question(
            question="Inspect second.",
            tool="read_file",
            args={"path": "src/app.py", "symbols": ["second"]},
        )
        full_question = state.evidence_ledger.register_question(
            question="Inspect the whole file.",
            tool="read_file",
            args={"path": "src/app.py"},
        )

        self._execute_read(
            executor,
            slice_question,
            path="src/app.py",
            symbols=["second"],
        )
        self._execute_read(executor, full_question, path="src/app.py")

        self.assertEqual(len(runtime.calls), 1)
        self.assertEqual(
            state.tool_events[0]["metadata"]["coverage_type"],
            "file_slice",
        )
        self.assertEqual(
            state.tool_events[1]["metadata"]["coverage_type"],
            "full_file",
        )
        self.assertFalse(state.tool_events[1]["metadata"]["backend_attempted"])
        self.assertIn("def first", state.collected_snippets[-1]["code"])
        self.assertIn("def second", state.collected_snippets[-1]["code"])
        self.assertNotIn("reused without a backend call", state.tool_events[1]["result_summary"])

    def test_prior_event_without_source_cache_materializes_backend_bytes(self):
        content = "def run():\n    return 1\n"
        runtime = _BoundedRuntime(
            {
                "src/app.py": {
                    "outcome": "success",
                    "content": content,
                    "source_size_bytes": len(content.encode()),
                    "bytes_read": len(content.encode()),
                    "max_bytes": 2 * 1024 * 1024,
                }
            }
        )
        state = _state(runtime, "src/app.py")
        prior_question = state.evidence_ledger.register_question(
            question="Historical observation.",
            tool="read_file",
            args={"path": "src/app.py"},
        )
        prior_event_id = state.evidence_ledger.record_event(
            question_id=prior_question,
            tool="read_file",
            args={"path": "src/app.py"},
            outcome="hit",
            paths=["src/app.py"],
            source_ref=f"pr_head:{state.head_sha}",
            coverage_type="full_file",
            observed_state="content_observed",
        )
        state.tool_events.append(
            {
                "tool": "read_file",
                "args": {"path": "src/app.py"},
                "outcome": "hit",
                "paths": ["src/app.py"],
                "source_ref": f"pr_head:{state.head_sha}",
                "head_reread_outcome": "hit",
                "evidence_event_id": prior_event_id,
                "metadata": {
                    "coverage_type": "full_file",
                    "backend_attempted": True,
                },
            }
        )
        current_question = state.evidence_ledger.register_question(
            question="Observe the exact current bytes.",
            tool="read_file",
            args={"path": "src/app.py"},
        )

        self._execute_read(
            ToolExecutor(state),
            current_question,
            path="src/app.py",
        )

        self.assertEqual(len(runtime.calls), 1)
        self.assertTrue(state.tool_events[-1]["metadata"]["backend_attempted"])
        self.assertIn(content, state.collected_snippets[-1]["code"])
        self.assertNotIn(
            "reused without a backend call",
            state.tool_events[-1]["result_summary"],
        )

    def test_fetch_health_truth_table_is_independent_of_pr_type(self):
        for successful, expected, outer_status in (
            (set(), "failed", "partial_or_failed_context"),
            ({"a.py"}, "degraded", "partial_or_failed_context"),
            ({"a.py", "b.py"}, "complete", "healthy"),
        ):
            with self.subTest(expected=expected):
                state = _state(_BoundedRuntime({}))
                state.planned_read_paths = ["a.py", "b.py"]
                state.read_success_paths = set(successful)
                for path in ("a.py", "b.py"):
                    args = {
                        "path": path,
                        "mode": "content",
                        "reason": "Verify retrieval health.",
                    }
                    question_id = state.evidence_ledger.register_question(
                        question=f"Inspect {path}.",
                        tool="read_file",
                        args=args,
                    )
                    state.evidence_ledger.record_event(
                        question_id=question_id,
                        tool="read_file",
                        args=args,
                        outcome="hit" if path in successful else "error",
                        paths=[path] if path in successful else [],
                        source_ref=f"pr_head:{state.head_sha}",
                        coverage_type=(
                            "full_file" if path in successful else ""
                        ),
                        observed_state=(
                            "content_observed"
                            if path in successful
                            else "content_unobserved"
                        ),
                    )
                health = _fetch_health(
                    state,
                    {
                        "pr_type": "dependency",
                        "verification_plan": [
                            {"tool": "read_file", "args": {"path": "a.py"}},
                            {"tool": "read_file", "args": {"path": "b.py"}},
                        ],
                    },
                )
                self.assertEqual(health["planned_retrieval_status"], expected)
                self.assertEqual(health["status"], outer_status)

        empty = _state(_BoundedRuntime({}))
        health = _fetch_health(empty, {"pr_type": "code", "verification_plan": []})
        self.assertEqual(health["planned_retrieval_status"], "not_requested")

    def test_fetch_health_reasons_follow_typed_lifecycle_without_false_read_failure(self):
        no_hit_state = _state(_BoundedRuntime({}))
        search_question = no_hit_state.evidence_ledger.register_question(
            question="Find exact callers.",
            tool="search_code",
            args={"query": "WidgetFactory", "reason": "Find callers."},
        )
        no_hit_state.evidence_ledger.record_event(
            question_id=search_question,
            tool="search_code",
            args={"query": "WidgetFactory", "reason": "Find callers."},
            outcome="no_hit",
            source_ref=f"pr_head:{no_hit_state.head_sha}",
            coverage_type="search_snippet",
        )
        no_hit_health = _fetch_health(
            no_hit_state,
            {
                "pr_type": "code",
                "verification_plan": [
                    {
                        "question_id": search_question,
                        "tool": "search_code",
                        "args": {
                            "query": "WidgetFactory",
                            "reason": "Find callers.",
                        },
                    }
                ],
            },
        )
        self.assertEqual(no_hit_health["planned_retrieval_status"], "complete")
        self.assertEqual(no_hit_health["status"], "healthy")
        self.assertEqual(no_hit_health["supporting_question_count"], 0)
        self.assertEqual(no_hit_health["degradation_reason_counts"], {})
        self.assertNotIn("read_failure", no_hit_health["reasons"])

        for tool, args in (
            (
                "search_code",
                {"query": "WidgetFactory", "reason": "Find callers."},
            ),
            (
                "list_dir",
                {"target_path": "src", "max_depth": 1},
            ),
        ):
            with self.subTest(tool=tool, outcome="quota_exhausted"):
                quota_state = _state(_BoundedRuntime({}))
                quota_question = quota_state.evidence_ledger.register_question(
                    question=f"Run quota-limited {tool}.",
                    tool=tool,
                    args=args,
                )
                quota_state.evidence_ledger.record_event(
                    question_id=quota_question,
                    tool=tool,
                    args=args,
                    outcome="quota_exhausted",
                )
                quota_health = _fetch_health(
                    quota_state,
                    {
                        "pr_type": "code",
                        "verification_plan": [
                            {
                                "question_id": quota_question,
                                "tool": tool,
                                "args": args,
                            }
                        ],
                    },
                )
                self.assertEqual(
                    quota_health["degradation_reason_counts"],
                    {"quota_or_budget_exhausted": 1},
                )
                self.assertNotIn("search_error", quota_health["reasons"])

        cases = (
            ("dropped_invalid", "invalid_plan", "read_file"),
            ("budget_skipped", "budget_skipped", "read_file"),
            ("terminal_unexecuted", "terminal_unexecuted", "list_dir"),
        )
        for lifecycle, expected_reason, tool in cases:
            with self.subTest(lifecycle=lifecycle):
                state = _state(_BoundedRuntime({}), "src/app.py")
                args = (
                    {"path": "src/app.py"}
                    if tool == "read_file"
                    else {"target_path": "src", "max_depth": 1}
                )
                question_id = state.evidence_ledger.register_question(
                    question=f"Verify {lifecycle}.",
                    tool=tool,
                    args=args,
                    lifecycle=lifecycle,
                )
                health = _fetch_health(
                    state,
                    {
                        "pr_type": "code",
                        "verification_plan": [
                            {
                                "question_id": question_id,
                                "tool": tool,
                                "args": args,
                            }
                        ],
                    },
                )
                self.assertEqual(
                    health["degradation_reason_counts"],
                    {expected_reason: 1},
                )
                self.assertNotIn("read_failure", health["reasons"])

        error_state = _state(_BoundedRuntime({}))
        error_question = error_state.evidence_ledger.register_question(
            question="Find exact callers.",
            tool="search_code",
            args={"query": "WidgetFactory", "reason": "Find callers."},
        )
        error_state.evidence_ledger.record_event(
            question_id=error_question,
            tool="search_code",
            args={"query": "WidgetFactory", "reason": "Find callers."},
            outcome="error",
            source_ref=f"pr_head:{error_state.head_sha}",
            coverage_type="search_snippet",
        )
        error_health = _fetch_health(
            error_state,
            {
                "pr_type": "code",
                "verification_plan": [
                    {
                        "question_id": error_question,
                        "tool": "search_code",
                        "args": {
                            "query": "WidgetFactory",
                            "reason": "Find callers.",
                        },
                    }
                ],
            },
        )
        self.assertEqual(
            error_health["degradation_reason_counts"],
            {"search_error": 1},
        )
        self.assertNotIn("read_failure", error_health["reasons"])


if __name__ == "__main__":
    unittest.main()
