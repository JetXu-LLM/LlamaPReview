import json
import logging
import re
import sys
import time
import unittest
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

from tests.unit.fakes import ensure_repo_root_on_path, install_fake_requests_module, set_default_env

ensure_repo_root_on_path()
set_default_env()
install_fake_requests_module()

from lambdas.LlamaPReviewPipeline.context_engine.state import CollectionState
from lambdas.LlamaPReviewPipeline.context_engine.assembler import assemble_context, context_meta
from lambdas.LlamaPReviewPipeline.context_engine.code_extractor import (
    CodeContextExtractor,
    extract_diff_entities,
)
from lambdas.LlamaPReviewPipeline.context_engine.evidence import (
    EvidenceLedger,
    stable_id,
)
from lambdas.LlamaPReviewPipeline.context_engine.pfr import (
    _cap_planned_steps,
    _ordered_steps,
    _terminal_evidence_read,
    collect_context_pfr,
)
from lambdas.LlamaPReviewPipeline.context_engine.pfr.evidence_execution import (
    _read_step_can_expand_evidence,
)
from lambdas.LlamaPReviewPipeline.context_engine.search_rag import (
    postprocess_search_args,
    postprocess_search_candidates,
)
from lambdas.LlamaPReviewPipeline.context_engine.tools import TOOLS, ToolExecutor
from lambdas.LlamaPReviewPipeline.deepseek_client import (
    DeepSeekHTTPError,
    ProviderCallFenceError,
    ProviderCallLedgerError,
    ProviderDispatchOutcomeUnknown,
)


class _Runtime:
    def __init__(self):
        self.read_calls = []
        self.search_calls = []

    def search_code(self, query, repo):
        self.search_calls.append((query, repo))
        return [{"index": 0, "path": "src/service.py", "content": "def run():\n    return do_work()\n"}]

    def get_file_content(self, repo, path, *, sha=None):
        self.read_calls.append((repo, path, sha))
        if path == "AGENTS.md":
            return "Run the fast test suite before recommending merge."
        return "def run():\n    return do_work()\n"


class _FakePfrClient:
    def __init__(
        self,
        responses=None,
        fail_all=False,
        failure_message="model unavailable",
        failure_exception=None,
    ):
        self.responses = list(responses or [])
        self.fail_all = fail_all
        self.failure_message = failure_message
        self.failure_exception = failure_exception
        self.calls = []

    def chat(self, messages, **kwargs):
        self.calls.append({"messages": messages, **kwargs})
        if self.fail_all:
            if self.failure_exception is not None:
                raise self.failure_exception
            raise RuntimeError(self.failure_message)
        payload = self.responses.pop(0)
        if isinstance(payload, BaseException):
            raise payload
        return {
            "choices": [
                {
                    "message": {"role": "assistant", "content": json.dumps(payload)},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"total_tokens": 11},
        }


def _repo_tree(*paths):
    return {
        "files": [{"path": path} for path in paths],
        "tree": "\n".join(paths),
    }


def _pfr_pr_content():
    return {
        "pr_metadata": {"number": 12, "title": "change service"},
        "file_changes": [
            {
                "file_path": "src/service.py",
                "change_type": "modified",
                "additions": 1,
                "deletions": 1,
                "diff": "@@ -1 +1 @@\n-def run():\n+def run():\n+    return do_work()\n",
            }
        ],
    }


class TestContextEnginePipeline(unittest.TestCase):
    def test_symbol_slice_honors_exported_async_typescript_definition(self):
        content = """\
export const REQUIRED_SCOPE = 'scope'
async function mintToken() {
  return null
}

export async function getToken(scope: string = REQUIRED_SCOPE) {
  try {
    return await mintToken()
  } catch {
    return null
  }
}
"""
        sdk_package = ModuleType("llama_github")
        sdk_utils = ModuleType("llama_github.utils")
        sdk_utils.DiffGenerator = SimpleNamespace(
            _FUNC_CONTEXT_PATTERNS=[re.compile(r"^\s*(?:def|class)\s+")]
        )
        with patch.dict(
            sys.modules,
            {
                "llama_github": sdk_package,
                "llama_github.utils": sdk_utils,
            },
        ):
            extractor = CodeContextExtractor()
        block, start, end = extractor.extract_enclosing_block(
            content,
            5,
            "getToken",
        )

        self.assertEqual(start, 6)
        self.assertEqual(end, 12)
        self.assertTrue(block.startswith("export async function getToken"))
        self.assertIn("catch", block)
        self.assertNotIn("async function mintToken", block)

    def test_tools_schema_has_exact_four_tools(self):
        names = [tool["function"]["name"] for tool in TOOLS]
        self.assertEqual(names, ["search_code", "read_file", "list_dir", "finish_context"])
        search_schema = TOOLS[0]["function"]["parameters"]["properties"]
        self.assertIn("intent", search_schema)
        read_schema = TOOLS[1]["function"]["parameters"]["properties"]
        self.assertEqual(read_schema["symbols"]["maxItems"], 5)

    def test_branch_correctness_for_search_and_read_file(self):
        runtime = _Runtime()
        state = CollectionState(
            pr_details="details",
            pr_content={"file_changes": []},
            repo_full_name="owner/repo",
            head_sha="abcdef123456",
            default_branch="main",
            runtime=runtime,
            accessible_files={"src/service.py"},
        )
        executor = ToolExecutor(state, github_token="token")
        search_result = executor.execute(
            {
                "id": "1",
                "function": {
                    "name": "search_code",
                    "arguments": json.dumps({"query": "do_work(", "reason": "find callers"}),
                },
            }
        )
        self.assertIn("default branch", search_result)
        self.assertIn("PR head abcdef12", search_result)
        self.assertIn(("owner/repo", "src/service.py", "abcdef123456"), runtime.read_calls)

        read_result = executor.execute(
            {
                "id": "2",
                "function": {
                    "name": "read_file",
                    "arguments": json.dumps({"path": "src/service.py", "symbols": ["run"]}),
                },
            }
        )
        self.assertIn("[source: PR head abcdef12]", read_result)
        self.assertIn(("owner/repo", "src/service.py", "abcdef123456"), runtime.read_calls)
        self.assertEqual([event["tool"] for event in state.tool_events], ["search_code", "read_file"])
        self.assertEqual(state.tool_events[0]["args"]["reason"], "find callers")
        self.assertEqual(state.tool_events[0]["outcome"], "hit")
        self.assertGreaterEqual(state.tool_events[0]["hit_count"], 1)

    def test_repeated_read_and_search_do_not_consume_quota(self):
        runtime = _Runtime()
        state = CollectionState(
            pr_details="details",
            pr_content={"file_changes": []},
            repo_full_name="owner/repo",
            head_sha="abcdef123456",
            default_branch="main",
            runtime=runtime,
            accessible_files={"src/service.py"},
            max_read_calls=1,
            max_search_calls=1,
        )
        executor = ToolExecutor(state)

        executor.execute({"id": "1", "function": {"name": "read_file", "arguments": json.dumps({"path": "src/service.py"})}})
        repeat_read = executor.execute({"id": "2", "function": {"name": "read_file", "arguments": json.dumps({"path": "src/service.py"})}})
        executor.execute({"id": "3", "function": {"name": "search_code", "arguments": json.dumps({"query": "do_work(", "reason": "first"})}})
        repeat_search = executor.execute({"id": "4", "function": {"name": "search_code", "arguments": json.dumps({"query": "do_work(", "reason": "same symbol"})}})

        self.assertEqual(state.read_calls, 1)
        self.assertEqual(state.search_calls, 1)
        self.assertEqual(len(runtime.read_calls), 2)  # first read plus PR-head merge from search
        self.assertEqual(len(runtime.search_calls), 1)
        self.assertIn("already collected", repeat_read)
        self.assertIn("already searched", repeat_search)
        self.assertEqual(state.tool_events[1]["outcome"], "repeat")
        self.assertEqual(state.tool_events[3]["outcome"], "repeat")

    def test_path_guard_blocks_non_accessible_file(self):
        state = CollectionState(
            pr_details="details",
            pr_content={},
            repo_full_name="owner/repo",
            head_sha="abcdef123456",
            default_branch="main",
            runtime=_Runtime(),
            accessible_files={"src/service.py"},
        )
        result = ToolExecutor(state).execute(
            {"id": "3", "function": {"name": "read_file", "arguments": json.dumps({"path": "missing.py"})}}
        )
        self.assertIn("path guard", result)
        self.assertIn("missing.py", state.non_existent_files)
        self.assertEqual(state.tool_events[0]["tool"], "read_file")
        self.assertEqual(state.tool_events[0]["invalid_paths"], ["missing.py"])
        self.assertEqual(state.read_error_paths, {"missing.py"})

    def test_read_file_target_path_is_repaired_at_tool_boundary(self):
        runtime = _Runtime()
        state = CollectionState(
            pr_details="details",
            pr_content={},
            repo_full_name="owner/repo",
            head_sha="abcdef123456",
            default_branch="main",
            runtime=runtime,
            accessible_files={"src/service.py"},
        )

        result = ToolExecutor(state).execute(
            {
                "id": "repair",
                "function": {
                    "name": "read_file",
                    "arguments": json.dumps({"target_path": "src/service.py", "max_depth": 2, "reason": "Inspect file."}),
                },
            }
        )

        self.assertIn("[source: PR head abcdef12]", result)
        self.assertEqual(state.tool_arg_repair_counts["read_file.path_from_target_path"], 1)
        self.assertEqual(state.tool_arg_repair_counts["read_file.dropped_max_depth"], 1)
        self.assertEqual(state.tool_events[0]["args"]["path"], "src/service.py")
        self.assertNotIn("target_path", state.tool_events[0]["args"])

    def test_finish_context_sets_finish_reason_and_meta(self):
        state = CollectionState(
            pr_details="details",
            pr_content={"file_changes": []},
            repo_full_name="owner/repo",
            head_sha="abcdef123456",
            default_branch="main",
            runtime=_Runtime(),
        )
        result = ToolExecutor(state).execute(
            {
                "id": "4",
                "function": {
                    "name": "finish_context",
                    "arguments": json.dumps({"summary": "Enough context.", "known_gaps": ["Need smoke test evidence."]}),
                },
            }
        )

        self.assertIn("finished", result)
        self.assertTrue(state.finished)
        self.assertEqual(state.finish_reason, "explicit_finish")
        meta = context_meta(state)
        self.assertTrue(meta["finished"])
        self.assertEqual(meta["finish_reason"], "explicit_finish")
        self.assertEqual(meta["tool_event_count"], 1)

    def test_assembled_context_includes_compact_tool_trace(self):
        state = CollectionState(
            pr_details="details",
            pr_content={"file_changes": [{"file_path": "src/service.py", "change_type": "modified", "additions": 1, "deletions": 0}]},
            repo_full_name="owner/repo",
            head_sha="abcdef123456",
            default_branch="main",
            runtime=_Runtime(),
            accessible_files={"src/service.py"},
        )
        ToolExecutor(state).execute(
            {
                "id": "5",
                "function": {
                    "name": "read_file",
                    "arguments": json.dumps({"path": "src/service.py", "symbols": ["run"]}),
                },
            }
        )
        state.budget_exhausted_flag = True
        state.finish_reason = "budget_exhausted"

        context = assemble_context(state)
        meta = context_meta(state)

        self.assertIn("## Tool Trace", context)
        self.assertIn("`read_file`", context)
        self.assertIn("Finish reason: budget_exhausted", context)
        self.assertIn("Tool outcomes:", context)
        self.assertTrue(meta["budget_exhausted"])
        self.assertEqual(meta["tool_counts"]["read_file"], 1)
        self.assertEqual(meta["tool_outcome_counts"]["hit"], 1)

    def test_context_meta_records_successful_list_dir_paths(self):
        state = CollectionState(
            pr_details="details",
            pr_content={"file_changes": []},
            repo_full_name="owner/repo",
            head_sha="abcdef123456",
            default_branch="main",
            runtime=_Runtime(),
        )

        from lambdas.LlamaPReviewPipeline.context_engine import tools

        original = tools.get_repo_structure_for_llm
        tools.get_repo_structure_for_llm = lambda *_args, **_kwargs: {
            "tree": "|-- components/\n|   `-- Widget.tsx (120B)\n`-- package.json (372B)"
        }
        try:
            ToolExecutor(state).execute(
                {
                    "id": "list",
                    "function": {
                        "name": "list_dir",
                        "arguments": json.dumps({"target_path": "frontend", "max_depth": 2}),
                    },
                }
            )
        finally:
            tools.get_repo_structure_for_llm = original

        meta = context_meta(state)
        self.assertEqual(meta["tool_counts"]["list_dir"], 1)
        self.assertIn("frontend", meta["list_dir_paths"])
        self.assertIn("frontend/components", meta["list_dir_paths"])
        self.assertNotIn("components", meta["list_dir_paths"])
        self.assertNotIn("package.json (372B)", state.tool_events[0]["paths"])

    def test_empty_list_dir_does_not_record_placeholder_path(self):
        state = CollectionState(
            pr_details="details",
            pr_content={"file_changes": []},
            repo_full_name="owner/repo",
            head_sha="abcdef123456",
            default_branch="main",
            runtime=_Runtime(),
        )

        from lambdas.LlamaPReviewPipeline.context_engine import tools

        original = tools.get_repo_structure_for_llm
        tools.get_repo_structure_for_llm = lambda *_args, **_kwargs: {
            "tree": "[Empty or no items match criteria in: frontend/__tests__]"
        }
        try:
            ToolExecutor(state).execute(
                {
                    "id": "empty-list",
                    "function": {
                        "name": "list_dir",
                        "arguments": json.dumps({"target_path": "frontend/__tests__", "max_depth": 2}),
                    },
                }
            )
        finally:
            tools.get_repo_structure_for_llm = original

        meta = context_meta(state)
        self.assertEqual(state.tool_events[0]["outcome"], "no_hit")
        self.assertEqual(state.tool_events[0]["paths"], [])
        self.assertEqual(meta["list_dir_paths"], [])

    def test_query_postprocess_preserves_model_literals_and_diff_floors(self):
        pr_content = {
            "file_changes": [
                {
                    "file_path": "src/service.py",
                    "diff": "@@ -1 +1 @@\n-def old_handler():\n+def build_cache(timeout=None):\n+    return timeout\n",
                }
            ]
        }
        entities = {
            "added_symbols": {"build_cache"},
            "removed_symbols": {"old_handler"},
            "added_params": {"timeout"},
            "parameter_adoptions": {("build_cache", "timeout")},
        }

        queries, debug = postprocess_search_args(
            [
                {"query": "class WidgetManager", "reason": "Find usages.", "intent": "external_usage"},
                {"query": "List", "reason": "Noise.", "intent": "external_usage"},
                {"query": "extends WidgetManager", "reason": "Cross-language noise.", "intent": "interface_implementations"},
            ],
            entities=entities,
            pr_content=pr_content,
            max_total=6,
        )

        rendered_queries = [item["query"] for item in queries]
        self.assertEqual(
            rendered_queries,
            [
                "old_handler",
                "class WidgetManager",
                "List",
                "extends WidgetManager",
                "build_cache timeout",
            ],
        )
        self.assertTrue(all("language" not in item for item in debug))
        self.assertTrue(all("noise" not in item for item in debug))

    def test_query_postprocess_candidates_keep_stable_source_identity(self):
        candidates, _debug = postprocess_search_candidates(
            [
                {
                    "query": "class Worker",
                    "reason": "Find worker usage.",
                    "intent": "external_usage",
                }
            ],
            entities={
                "added_symbols": set(),
                "removed_symbols": {"run"},
                "added_params": set(),
                "parameter_adoptions": set(),
                "injected_callees": set(),
            },
            pr_content={"file_changes": [{"file_path": "src/service.py"}]},
            max_total=3,
        )

        self.assertEqual(
            [
                (item.origin_kind, item.origin_index, item.args["query"])
                for item in candidates
            ],
            [
                ("removed_symbol", 0, "run"),
                ("model", 0, "class Worker"),
            ],
        )

    def test_query_postprocess_distinguishes_cap_from_invalid_and_redundant(self):
        dispositions = {}
        candidates, _debug = postprocess_search_candidates(
            [
                {"query": "Worker", "reason": "First.", "intent": "external_usage"},
                {"query": "Worker", "reason": "Duplicate.", "intent": "external_usage"},
                {"query": "Builder", "reason": "Over cap.", "intent": "external_usage"},
            ],
            entities={
                "added_symbols": set(),
                "removed_symbols": set(),
                "added_params": set(),
                "parameter_adoptions": set(),
                "injected_callees": set(),
            },
            pr_content={"file_changes": [{"file_path": "src/service.py"}]},
            max_total=2,
            model_lifecycles=dispositions,
        )

        self.assertEqual([item.args["query"] for item in candidates], ["Worker", "Builder"])
        self.assertEqual(
            dispositions,
            {0: "kept", 1: "dropped_redundant", 2: "kept"},
        )

        capped = {}
        postprocess_search_candidates(
            [
                {"query": "Worker", "reason": "First.", "intent": "external_usage"},
                {"query": "Builder", "reason": "Over cap.", "intent": "external_usage"},
            ],
            entities={
                "added_symbols": set(),
                "removed_symbols": set(),
                "added_params": set(),
                "parameter_adoptions": set(),
                "injected_callees": set(),
            },
            pr_content={"file_changes": [{"file_path": "src/service.py"}]},
            max_total=1,
            model_lifecycles=capped,
        )
        self.assertEqual(capped, {0: "kept", 1: "dropped_cap"})

    def test_query_postprocess_reserves_removed_symbol_within_total_cap(self):
        candidates, _debug = postprocess_search_candidates(
            [
                {
                    "query": "UnrelatedHelper",
                    "reason": "Inspect another symbol.",
                    "intent": "external_usage",
                }
            ],
            entities={
                "added_symbols": set(),
                "removed_symbols": {"RemovedAPI"},
                "added_params": set(),
                "parameter_adoptions": set(),
                "injected_callees": set(),
            },
            pr_content={"file_changes": [{"file_path": "src/service.py"}]},
            max_total=1,
        )

        self.assertEqual([item.args["query"] for item in candidates], ["RemovedAPI"])
        self.assertEqual(candidates[0].origin_kind, "removed_symbol")

    def test_model_query_covering_removed_symbol_keeps_planner_identity(self):
        dispositions = {}
        candidates, debug = postprocess_search_candidates(
            [
                {
                    "query": "RemovedAPI(",
                    "reason": "Check whether the deleted public entry point still has callers.",
                    "intent": "external_usage",
                }
            ],
            entities={
                "added_symbols": set(),
                "removed_symbols": {"RemovedAPI"},
                "added_params": set(),
                "parameter_adoptions": set(),
                "injected_callees": set(),
            },
            pr_content={"file_changes": [{"file_path": "src/service.py"}]},
            max_total=1,
            model_lifecycles=dispositions,
        )

        self.assertEqual([item.args["query"] for item in candidates], ["RemovedAPI("])
        self.assertEqual(candidates[0].origin_kind, "model")
        self.assertEqual(candidates[0].origin_index, 0)
        self.assertEqual(candidates[0].args["intent"], "removal_cleanup")
        self.assertEqual(
            candidates[0].code_owned_priority,
            "diff_removed_symbol_floor",
        )
        self.assertEqual(dispositions, {0: "kept"})
        self.assertIn("add_model_removed_coverage:RemovedAPI(", debug)

    def test_model_exact_removed_symbol_floor_does_not_add_eager_variants(self):
        candidates, debug = postprocess_search_candidates(
            [
                {
                    "query": "RemovedWidget",
                    "reason": "Check whether the deleted component still has callers.",
                    # The model-authored intent is deliberately unrelated. The
                    # no-variant decision must come from the code-owned floor.
                    "intent": "external_usage",
                }
            ],
            entities={
                "added_symbols": set(),
                "removed_symbols": {"RemovedWidget"},
                "added_params": set(),
                "parameter_adoptions": set(),
                "injected_callees": set(),
            },
            pr_content={"file_changes": [{"file_path": "src/view.tsx"}]},
            max_total=4,
        )

        self.assertEqual([item.args["query"] for item in candidates], ["RemovedWidget"])
        self.assertEqual(candidates[0].origin_kind, "model")
        self.assertEqual(
            candidates[0].code_owned_priority,
            "diff_removed_symbol_floor",
        )
        self.assertFalse(any("variant" in item for item in debug))

    def test_model_removal_intent_without_code_owned_floor_does_not_gain_exemption(self):
        candidates, _debug = postprocess_search_candidates(
            [
                {
                    "query": "PlannerOnlyWidget",
                    "reason": "Planner labels this as cleanup without a matching diff removal.",
                    "intent": "removal_cleanup",
                }
            ],
            entities={
                "added_symbols": set(),
                "removed_symbols": set(),
                "added_params": set(),
                "parameter_adoptions": set(),
                "injected_callees": set(),
            },
            pr_content={"file_changes": [{"file_path": "src/view.tsx"}]},
            max_total=3,
        )

        self.assertEqual(
            [item.args["query"] for item in candidates],
            ["PlannerOnlyWidget"],
        )
        self.assertTrue(all(not item.code_owned_priority for item in candidates))

    def test_global_plan_cap_retains_one_removed_symbol_lookup(self):
        state = CollectionState(
            pr_details="details",
            pr_content={},
            repo_full_name="owner/repo",
            head_sha="head",
            default_branch="main",
            runtime=_Runtime(),
            max_read_calls=6,
            max_search_calls=1,
        )
        read_steps = [
            {
                "question": f"Read file {index}.",
                "tool": "read_file",
                "args": {"path": f"src/{index}.py"},
            }
            for index in range(6)
        ]
        removal = {
            "question": "Check removed API callers.",
            "tool": "search_code",
            "args": {
                "query": "RemovedAPI",
                "intent": "removal_cleanup",
            },
            "_priority_class": "diff_removed_symbol_floor",
        }

        kept = _cap_planned_steps([*read_steps, removal], state)

        self.assertEqual(len(kept), 7)
        self.assertIn(removal, kept)
        self.assertEqual(
            sum(step["tool"] == "read_file" for step in kept),
            6,
        )
        ordered = _ordered_steps(kept)
        self.assertIs(ordered[0], removal)

        fake_model_removal = {
            "question": "Unrelated model-authored priority claim.",
            "tool": "search_code",
            "args": {
                "query": "UnrelatedHelper",
                "intent": "removal_cleanup",
            },
        }
        self.assertEqual(
            _ordered_steps([read_steps[0], fake_model_removal])[0]["tool"],
            "read_file",
        )

        semantic_order = [
            {
                "question": "Verify the author's stated acceptance criterion.",
                "tool": "search_code",
                "args": {"query": "acceptance", "intent": "general"},
            },
            {
                "question": "Inspect the highest-consequence directory.",
                "tool": "list_dir",
                "args": {"path": "src"},
            },
            {
                "question": "Read a general follow-up file.",
                "tool": "read_file",
                "args": {"path": "src/general.py"},
            },
        ]
        self.assertEqual(_ordered_steps(semantic_order), semantic_order)

    def test_generic_removed_symbol_survives_model_noise_filter(self):
        candidates, _ = postprocess_search_candidates(
            [],
            entities={
                "added_symbols": set(),
                "removed_symbols": {"Config"},
                "added_params": set(),
                "parameter_adoptions": set(),
                "injected_callees": set(),
            },
            pr_content={"file_changes": [{"file_path": "src/service.py"}]},
            max_total=1,
        )

        self.assertEqual([item.args["query"] for item in candidates], ["Config"])
        self.assertEqual(candidates[0].origin_kind, "removed_symbol")

    def test_query_lifecycle_classifies_invalid_and_duplicate_before_cap(self):
        dispositions = {}
        postprocess_search_candidates(
            [
                {"query": "Worker", "reason": "First.", "intent": "external_usage"},
                {"query": "Worker", "reason": "Duplicate.", "intent": "external_usage"},
                {"query": "import {", "reason": "Invalid.", "intent": "external_usage"},
                {"query": "Builder", "reason": "Over cap.", "intent": "external_usage"},
            ],
            entities={
                "added_symbols": set(),
                "removed_symbols": set(),
                "added_params": set(),
                "parameter_adoptions": set(),
                "injected_callees": set(),
            },
            pr_content={"file_changes": [{"file_path": "src/service.py"}]},
            max_total=1,
            model_lifecycles=dispositions,
        )

        self.assertEqual(
            dispositions,
            {
                0: "kept",
                1: "dropped_redundant",
                2: "dropped_invalid",
                3: "dropped_cap",
            },
        )

    def test_query_postprocess_does_not_generate_language_variants(self):
        candidates, _debug = postprocess_search_candidates(
            [
                {"query": "worker", "reason": "Calls.", "intent": "external_usage"},
                {"query": "Builder", "reason": "Usages.", "intent": "external_usage"},
            ],
            entities={
                "added_symbols": set(),
                "removed_symbols": set(),
                "added_params": set(),
                "parameter_adoptions": set(),
                "injected_callees": set(),
            },
            pr_content={"file_changes": [{"file_path": "src/view.tsx"}]},
            max_total=8,
        )

        self.assertEqual(
            [item.args["query"] for item in candidates],
            ["worker", "Builder"],
        )
        self.assertTrue(all(item.origin_kind == "model" for item in candidates))

    def test_query_postprocess_drops_degenerate_parameter_adoption(self):
        queries, debug = postprocess_search_args(
            [
                {
                    "query": "abs abs",
                    "reason": "Check adoption of a parameter.",
                    "intent": "parameter_adoption",
                }
            ],
            entities={
                "added_symbols": {"abs"},
                "removed_symbols": set(),
                "added_params": set(),
                "parameter_adoptions": set(),
            },
            pr_content={"file_changes": [{"file_path": "src/index.js"}]},
            max_total=4,
        )

        self.assertEqual(queries, [])
        self.assertEqual(debug, ["drop_degenerate_parameter_adoption:abs"])

    def test_diff_member_calls_do_not_create_code_owned_search_seeds(self):
        pr_content = {
            "file_changes": [
                {
                    "file_path": "routes/adapter.js",
                    "diff": (
                        "+module.exports = async function adapter(req, res, deps) {\n"
                        "+  const { sendJson, collectRequestBody, handleStreamChat } = deps;\n"
                        "+  const body = await collectRequestBody(req);\n"
                        "+  await handleStreamChat(body, res);\n"
                        "+  sendJson(res, 200, body);\n"
                        "+  res.write('done');\n"
                        "+}\n"
                    ),
                }
            ]
        }
        entities = extract_diff_entities(pr_content)

        queries, _debug = postprocess_search_args(
            [],
            entities=entities,
            pr_content=pr_content,
            max_total=2,
        )

        self.assertNotIn("injected_callees", entities)
        self.assertEqual(queries, [])

    def test_query_postprocess_applies_only_syntax_normalization(self):
        pr_content = {"file_changes": [{"file_path": "src/app/admin/ops/page.tsx"}]}
        queries, debug = postprocess_search_args(
            [
                {"query": "_utf8.Reset()", "reason": "Find decoder reset.", "intent": "external_usage"},
                {"query": "(Brush)TryFindResource", "reason": "Find resource casts.", "intent": "external_usage"},
                {"query": "var(--color-brand)", "reason": "Find CSS variable.", "intent": "external_usage"},
                {"query": "import { TrendBarChart", "reason": "Find imported chart.", "intent": "peer_dependency"},
                {"query": "$queryRaw(", "reason": "Find raw SQL usage.", "intent": "external_usage"},
                {"query": "WidgetManager(", "reason": "Find widget callers.", "intent": "external_usage"},
            ],
            entities={"added_symbols": set(), "removed_symbols": set(), "added_params": set()},
            pr_content=pr_content,
            max_total=8,
        )

        self.assertEqual(
            [item["query"] for item in queries],
            [
                "_utf8.Reset()",
                "(Brush)TryFindResource",
                "var(--color-brand)",
                "$queryRaw(",
                "WidgetManager(",
            ],
        )
        self.assertIn(
            "drop_unsupported_metachar:'import { TrendBarChart'",
            debug,
        )
        self.assertFalse(any("cast" in item or "css" in item for item in debug))

    def test_query_postprocess_preserves_exact_cli_or_config_token(self):
        pr_content = {
            "file_changes": [
                {"file_path": ".github/workflows/release.yml", "diff": "+run: release_cli"}
            ]
        }
        raw = [
            {
                "query": "release_cli",
                "reason": "Find the referenced release command and its implementation.",
                "intent": "external_usage",
            }
        ]
        entities = {
            "added_symbols": set(),
            "removed_symbols": set(),
            "added_params": set(),
        }

        queries, debug = postprocess_search_args(
            raw,
            entities=entities,
            pr_content=pr_content,
            max_total=2,
        )
        self.assertEqual(
            [item["query"] for item in queries],
            ["release_cli"],
        )
        self.assertFalse(any(item.startswith("prefer_call_form:") for item in debug))

        one_slot, _debug = postprocess_search_args(
            raw,
            entities=entities,
            pr_content=pr_content,
            max_total=1,
        )
        self.assertEqual([item["query"] for item in one_slot], ["release_cli"])

    def test_search_code_intent_prefers_external_hits_and_relaxes_when_needed(self):
        class Runtime:
            def __init__(self):
                self.search_calls = []

            def search_code(self, query, repo):
                self.search_calls.append(query)
                if query == "do_work(":
                    return [
                        {"index": 0, "path": "src/service.py", "content": "def run():\n    do_work()\n"},
                        {"index": 1, "path": "src/consumer.py", "content": "def call():\n    do_work()\n"},
                    ]
                return [{"index": 0, "path": "src/service.py", "content": "def run():\n    do_work()\n"}]

            def get_file_content(self, repo, path, *, sha=None):
                return "def call():\n    do_work()\n"

        runtime = Runtime()
        state = CollectionState(
            pr_details="details",
            pr_content={"file_changes": [{"file_path": "src/service.py"}]},
            repo_full_name="owner/repo",
            head_sha="abcdef123456",
            default_branch="main",
            runtime=runtime,
            accessible_files={"src/service.py", "src/consumer.py"},
        )
        executor = ToolExecutor(state)

        external = executor.execute(
            {
                "id": "search-external",
                "function": {
                    "name": "search_code",
                    "arguments": json.dumps({"query": "do_work(", "reason": "Find callers.", "intent": "external_usage"}),
                },
            }
        )
        self.assertIn("excluded 1 changed-file search hits", external)
        self.assertIn("src/consumer.py", external)
        self.assertNotIn("src/service.py:1-2", external)

        state.attempted_search_queries.clear()
        relaxed = executor.execute(
            {
                "id": "search-relaxed",
                "function": {
                    "name": "search_code",
                    "arguments": json.dumps({"query": "only_changed(", "reason": "Find callers.", "intent": "external_usage"}),
                },
            }
        )
        self.assertIn("relaxed modified-file filter", relaxed)
        self.assertIn("src/service.py", relaxed)

    def test_search_code_does_not_generate_an_implicit_second_pass(self):
        class Runtime:
            def __init__(self):
                self.search_calls = []

            def search_code(self, query, repo):
                self.search_calls.append(query)
                if query == "function buildCache":
                    return [{"index": 0, "path": "src/service.ts", "content": "export function buildCache() {\n  return 1\n}\n"}]
                return []

            def get_file_content(self, repo, path, *, sha=None):
                return "export function buildCache() {\n  return 1\n}\n"

        runtime = Runtime()
        state = CollectionState(
            pr_details="details",
            pr_content={"file_changes": [{"file_path": "src/service.ts"}]},
            repo_full_name="owner/repo",
            head_sha="abcdef123456",
            default_branch="main",
            runtime=runtime,
            accessible_files={"src/service.ts"},
            max_search_calls=2,
        )
        result = ToolExecutor(state).execute(
            {
                "id": "search-second-pass",
                "function": {
                    "name": "search_code",
                    "arguments": json.dumps({"query": "buildCache", "reason": "Find symbol.", "intent": "external_usage"}),
                },
            }
        )

        self.assertEqual(runtime.search_calls, ["buildCache"])
        self.assertEqual(state.search_calls, 1)
        self.assertNotIn("second_pass", result)

    def test_removed_symbol_no_hit_does_not_spend_quota_on_narrow_variants(self):
        class Runtime:
            def __init__(self):
                self.search_calls = []

            def search_code(self, query, repo):
                self.search_calls.append(query)
                return []

        runtime = Runtime()
        state = CollectionState(
            pr_details="details",
            pr_content={"file_changes": [{"file_path": "src/service.py"}]},
            repo_full_name="owner/repo",
            head_sha="abcdef123456",
            default_branch="main",
            runtime=runtime,
            max_search_calls=6,
        )
        executor = ToolExecutor(state)

        executor.execute(
            {
                "id": "removed-search",
                "function": {
                    "name": "search_code",
                    "arguments": json.dumps(
                        {
                            "query": "RemovedAPI",
                            "reason": "Find lingering callers.",
                            "intent": "removal_cleanup",
                        }
                    ),
                },
            }
        )
        executor.execute(
            {
                "id": "independent-search",
                "function": {
                    "name": "search_code",
                    "arguments": json.dumps(
                        {
                            "query": "OtherQuestion",
                            "reason": "Answer an independent planner question.",
                            "intent": "external_usage",
                        }
                    ),
                },
            }
        )

        self.assertEqual(runtime.search_calls[0], "RemovedAPI")
        self.assertEqual(runtime.search_calls[1], "OtherQuestion")
        self.assertNotIn("class RemovedAPI", runtime.search_calls)
        self.assertNotIn("function RemovedAPI", runtime.search_calls)

    def test_search_code_exception_records_search_error_not_no_hit(self):
        class Runtime:
            def search_code(self, query, repo):
                raise RuntimeError("HTTP 422 validation failed")

            def get_file_content(self, repo, path, *, sha=None):
                return None

        state = CollectionState(
            pr_details="details",
            pr_content={"file_changes": []},
            repo_full_name="owner/repo",
            head_sha="abcdef123456",
            default_branch="main",
            runtime=Runtime(),
            accessible_files=set(),
        )

        result = ToolExecutor(state).execute(
            {
                "id": "bad-search",
                "function": {
                    "name": "search_code",
                    "arguments": json.dumps({"query": "(Brush)TryFindResource", "reason": "Find resource calls."}),
                },
            }
        )
        meta = context_meta(state)

        self.assertIn("search_code error", result)
        self.assertEqual(state.tool_events[0]["outcome"], "search_error")
        self.assertEqual(state.no_hit_tool_calls, 0)
        self.assertEqual(meta["search_error_tool_calls"], 1)
        self.assertEqual(meta["tool_outcome_counts"]["search_error"], 1)

    def test_search_code_hit_ignores_error_substring_in_fetched_code(self):
        class Runtime:
            def search_code(self, query, repo):
                return [
                    {
                        "index": 0,
                        "path": "src/errors.js",
                        "content": "export function placeOrder() {\n  console.log(' error: handled upstream')\n}\n",
                    }
                ]

            def get_file_content(self, repo, path, *, sha=None):
                return "export function placeOrder() {\n  console.log(' error: handled upstream')\n}\n"

        state = CollectionState(
            pr_details="details",
            pr_content={"file_changes": [{"file_path": "src/errors.js"}]},
            repo_full_name="owner/repo",
            head_sha="abcdef123456",
            default_branch="main",
            runtime=Runtime(),
            accessible_files={"src/errors.js"},
        )

        ToolExecutor(state).execute(
            {
                "id": "search-hit-with-error-string",
                "function": {
                    "name": "search_code",
                    "arguments": json.dumps({"query": "placeOrder(", "reason": "Find order calls."}),
                },
            }
        )

        self.assertEqual(state.tool_events[0]["outcome"], "hit")
        self.assertEqual(state.search_error_tool_calls, 0)
        self.assertNotIn("error", context_meta(state)["tool_outcome_counts"])

    def test_snippet_parse_fallback_is_counted_without_error_log(self):
        class Runtime:
            def search_code(self, query, repo):
                logging.getLogger("llama_github.utils").error("Syntax error in the provided code: invalid syntax")
                return []

            def get_file_content(self, repo, path, *, sha=None):
                return None

        records = []

        class Capture(logging.Handler):
            def emit(self, record):
                records.append(record)

        llama_logger = logging.getLogger("llama_github.utils")
        handler = Capture()
        old_level = llama_logger.level
        llama_logger.setLevel(logging.DEBUG)
        llama_logger.addHandler(handler)
        try:
            state = CollectionState(
                pr_details="details",
                pr_content={"file_changes": []},
                repo_full_name="owner/repo",
                head_sha="abcdef123456",
                default_branch="main",
                runtime=Runtime(),
                accessible_files=set(),
                max_search_calls=1,
            )
            ToolExecutor(state).execute(
                {
                    "id": "snippet-fallback",
                    "function": {
                        "name": "search_code",
                        "arguments": json.dumps({"query": "WidgetManager(", "reason": "Find callers."}),
                    },
                }
            )
        finally:
            llama_logger.removeHandler(handler)
            llama_logger.setLevel(old_level)

        self.assertEqual(state.snippet_parse_fallbacks, 1)
        self.assertEqual(context_meta(state)["snippet_parse_fallbacks"], 1)
        self.assertFalse(any(record.levelno >= logging.ERROR for record in records))

    def test_search_code_with_status_error_records_search_error(self):
        class Runtime:
            def search_code_with_status(self, query, repo):
                return {"results": [], "error": "secondary rate limit", "status": 403}

            def get_file_content(self, repo, path, *, sha=None):
                return None

        state = CollectionState(
            pr_details="details",
            pr_content={"file_changes": []},
            repo_full_name="owner/repo",
            head_sha="abcdef123456",
            default_branch="main",
            runtime=Runtime(),
            accessible_files=set(),
        )

        result = ToolExecutor(state).execute(
            {
                "id": "rate-limited-search",
                "function": {
                    "name": "search_code",
                    "arguments": json.dumps({"query": "WidgetManager(", "reason": "Find callers."}),
                },
            }
        )

        self.assertIn("HTTP 403", result)
        self.assertEqual(state.tool_events[0]["outcome"], "search_error")
        self.assertEqual(state.search_error_queries[0]["query"], "WidgetManager(")

    def test_pfr_executes_plan_and_reconcile_with_owner_docs(self):
        runtime = _Runtime()
        client = _FakePfrClient(
            [
                {
                    "complexity": "normal",
                    "pr_type": "code",
                    "risk_domains": ["api"],
                    "verification_plan": [
                        {
                            "question": "Inspect the changed service file.",
                            "why_it_matters": "It contains the changed behavior.",
                            "tool": "read_file",
                            "args": {"path": "src/service.py"},
                        },
                        {
                            "question": "Find callers of do_work.",
                            "why_it_matters": "Callers reveal ripple effects.",
                            "tool": "search_code",
                            "args": {"query": "do_work(", "reason": "Find callers."},
                        },
                    ],
                },
                {
                    "summary": "The changed file and caller search were checked.",
                    "answered": [{"question": "Inspect the changed service file.", "evidence": "src/service.py"}],
                    "unresolved_gaps": [],
                    "followups": [],
                    "complete": True,
                },
            ]
        )

        from lambdas.LlamaPReviewPipeline.context_engine import initialization

        original = initialization.get_repo_structure_for_llm
        initialization.get_repo_structure_for_llm = lambda *_args, **kwargs: _repo_tree("src/service.py", "AGENTS.md")
        try:
            context, meta = collect_context_pfr(
                runtime=runtime,
                github_token="token",
                repo_full_name="owner/repo",
                pr_content=_pfr_pr_content(),
                pr_details="# PR\n",
                head_sha="abcdef123456",
                default_branch="main",
                client=client,
                route_plan={
                    "complexity": "normal",
                    "pr_type": "code",
                    "risk_domains": ["api"],
                },
                max_search_calls=2,
                max_read_calls=3,
            )
        finally:
            initialization.get_repo_structure_for_llm = original

        self.assertEqual(meta["context_strategy"], "pfr")
        self.assertEqual(meta["finish_reason"], "plan_complete")
        self.assertEqual(meta["pfr_plan"]["pr_type"], "code")
        self.assertEqual(meta["pfr_plan_status"], "model_ok")
        self.assertFalse(meta["plan_fallback_used"])
        self.assertIn("src/service.py", meta["changed_file_paths"])
        self.assertIn("src/service.py", meta["search_hit_paths"])
        self.assertIn("src/service.py", meta["evidence_hit_paths"])
        self.assertIn("Repo Fact Sheet", context)
        self.assertIn("Repository Review Guidance (untrusted evidence)", context)
        self.assertNotIn("Owner Instructions", context)
        self.assertIn("--- BEGIN OWNER DOC AGENTS.md ---", context)
        self.assertIn("Run the fast test suite", context)
        self.assertIn(("owner/repo", "src/service.py", "abcdef123456"), runtime.read_calls)
        self.assertTrue(any(call[0] == "do_work(" for call in runtime.search_calls))
        self.assertEqual([call["trace_phase"] for call in client.calls], ["pfr_plan", "pfr_reconcile"])

    def test_pfr_does_not_inject_definition_or_config_path_hints(self):
        runtime = _Runtime()
        client = _FakePfrClient(
            [
                {
                    "complexity": "normal",
                    "pr_type": "code",
                    "risk_domains": ["ui"],
                    "verification_plan": [
                        {
                            "question": "Read the token definition file.",
                            "why_it_matters": "The changed class depends on a theme token.",
                            "tool": "read_file",
                            "args": {"path": "tailwind.config.ts"},
                        }
                    ],
                },
                {"summary": "Token definition checked.", "answered": [], "unresolved_gaps": [], "followups": [], "complete": True},
            ]
        )
        pr_content = {
            "file_changes": [
                {
                    "file_path": "app/page.tsx",
                    "change_type": "modified",
                    "additions": 1,
                    "deletions": 0,
                    "diff": '@@ -1 +1 @@\n+<Button className="text-brand-foreground" />\n',
                }
            ]
        }

        from lambdas.LlamaPReviewPipeline.context_engine import initialization

        original = initialization.get_repo_structure_for_llm
        initialization.get_repo_structure_for_llm = lambda *_args, **kwargs: _repo_tree("app/page.tsx", "tailwind.config.ts")
        try:
            _context, meta = collect_context_pfr(
                runtime=runtime,
                github_token="token",
                repo_full_name="owner/repo",
                pr_content=pr_content,
                pr_details="# PR\n",
                head_sha="abcdef123456",
                default_branch="main",
                client=client,
            )
        finally:
            initialization.get_repo_structure_for_llm = original

        plan_prompt = "\n".join(
            message["content"] for message in client.calls[0]["messages"]
        )
        self.assertNotIn("Deterministic definition/config hints", plan_prompt)
        self.assertNotIn("definition_hint_paths", meta)
        self.assertIn("tailwind.config.ts", meta["planned_read_paths"])

    def test_pfr_soft_budget_forces_bounded_finish(self):
        class SlowRuntime(_Runtime):
            def get_file_content(self, repo, path, *, sha=None):
                time.sleep(0.01)
                return super().get_file_content(repo, path, sha=sha)

        runtime = SlowRuntime()
        client = _FakePfrClient(
            [
                {
                    "complexity": "normal",
                    "pr_type": "code",
                    "risk_domains": ["api"],
                    "verification_plan": [
                        {
                            "question": "Inspect the changed service file.",
                            "why_it_matters": "Changed code remains the floor.",
                            "tool": "read_file",
                            "args": {"path": "src/service.py"},
                        },
                        {
                            "question": "Inspect a related contract file.",
                            "why_it_matters": "It may reveal ripple effects.",
                            "tool": "read_file",
                            "args": {"path": "src/contract.py"},
                        },
                    ],
                },
                {"summary": "Partial evidence was collected.", "answered": [], "unresolved_gaps": [], "followups": [], "complete": True},
            ]
        )

        from lambdas.LlamaPReviewPipeline.context_engine import initialization

        original = initialization.get_repo_structure_for_llm
        initialization.get_repo_structure_for_llm = lambda *_args, **kwargs: _repo_tree("src/service.py", "src/contract.py")
        try:
            context, meta = collect_context_pfr(
                runtime=runtime,
                github_token="token",
                repo_full_name="owner/repo",
                pr_content=_pfr_pr_content(),
                pr_details="# PR\n",
                head_sha="abcdef123456",
                default_branch="main",
                client=client,
                soft_time_budget=0.001,
            )
        finally:
            initialization.get_repo_structure_for_llm = original

        self.assertTrue(meta["soft_budget_exhausted"])
        self.assertTrue(meta["budget_exhausted"])
        self.assertIn("soft_budget_exhausted", meta["budget_health_reasons"])
        self.assertIn("budget_skipped", meta["fetch_health"]["reasons"])
        self.assertIn(meta["finish_reason"], {"budget_exhausted", "partial_or_failed_context"})
        self.assertTrue(context.strip())

    def test_pfr_soft_budget_rescues_one_critical_followup_read_and_records_skipped_rest(self):
        class SlowRuntime(_Runtime):
            def get_file_content(self, repo, path, *, sha=None):
                time.sleep(0.01)
                return f"// {path}\nexport function checked() {{ return true }}\n"

        runtime = SlowRuntime()
        client = _FakePfrClient(
            [
                {
                    "complexity": "high",
                    "pr_type": "code",
                    "risk_domains": ["api"],
                    "verification_plan": [
                        {
                            "question": "Inspect the changed service file.",
                            "why_it_matters": "Changed code remains the floor.",
                            "tool": "read_file",
                            "args": {"path": "src/service.py"},
                        }
                    ],
                },
                {
                    "summary": "The service file was checked, but two contract reads could change merge judgment.",
                    "answered": [],
                    "unresolved_gaps": [],
                    "followups": [
                        {
                            "question": "Read the first contract needed for a candidate blocker.",
                            "tool": "read_file",
                            "args": {"path": "src/contract.py", "reason": "Confirm candidate blocker."},
                        },
                        {
                            "question": "Read the second contract needed for another candidate blocker.",
                            "tool": "read_file",
                            "args": {"path": "src/other_contract.py", "reason": "Confirm candidate blocker."},
                        },
                    ],
                    "complete": False,
                },
                {
                    "summary": "The critical rescue evidence was consumed in the terminal judgment.",
                    "answered": [],
                    "unresolved_gaps": [],
                    "followups": [],
                    "complete": True,
                },
            ]
        )

        from lambdas.LlamaPReviewPipeline.context_engine import initialization

        original = initialization.get_repo_structure_for_llm
        initialization.get_repo_structure_for_llm = lambda *_args, **kwargs: _repo_tree("src/service.py", "src/contract.py", "src/other_contract.py")
        try:
            _context, meta = collect_context_pfr(
                runtime=runtime,
                github_token="token",
                repo_full_name="owner/repo",
                pr_content=_pfr_pr_content(),
                pr_details="# PR\n",
                head_sha="abcdef123456",
                default_branch="main",
                client=client,
                soft_time_budget=0.001,
            )
        finally:
            initialization.get_repo_structure_for_llm = original

        self.assertTrue(meta["soft_budget_exhausted"])
        self.assertIn("src/contract.py", meta["read_success_paths"])
        self.assertNotIn("src/other_contract.py", meta["read_success_paths"])
        self.assertIn("src/other_contract.py", meta["budget_skipped_verification_paths"])
        self.assertIn("budget_skipped", meta["fetch_health"]["reasons"])
        self.assertEqual(
            [call["trace_phase"] for call in client.calls],
            ["pfr_plan", "pfr_reconcile"],
        )
        self.assertEqual(meta["pfr_terminal_reconcile_round"], 1)
        self.assertEqual(
            meta["pfr_terminal_reconcile_trigger"],
            "round_cap_or_failure",
        )
        self.assertFalse(meta["pfr_terminal_reconcile_covers_all_evidence"])
        self.assertTrue(meta["pfr_direct_evidence_only"])
        self.assertIn(
            "this reconciliation does not cover it",
            meta["pfr_reconcile"]["summary"],
        )
        self.assertEqual(meta["pfr_post_terminal_tool_call_count"], 0)

    def test_pfr_soft_budget_rescues_broader_slice_from_already_read_path(self):
        class SlowRuntime(_Runtime):
            def get_file_content(self, repo, path, *, sha=None):
                time.sleep(0.05)
                return (
                    "export const REQUIRED_SCOPE = 'scope'\n"
                    "async function mintToken() { return null }\n"
                    "export async function getToken() { return mintToken() }\n"
                )

        runtime = SlowRuntime()
        client = _FakePfrClient(
            [
                {
                    "complexity": "normal",
                    "pr_type": "code",
                    "risk_domains": ["api"],
                    "verification_plan": [
                        {
                            "question": "Inspect the deciding token contract.",
                            "why_it_matters": "It is Route's highest-consequence local fact.",
                            "tool": "read_file",
                            "args": {
                                "path": "src/token.ts",
                                "symbols": ["getToken", "REQUIRED_SCOPE"],
                            },
                        }
                    ],
                },
                {
                    "summary": "The wrapper body remains unresolved.",
                    "answered": [],
                    "unresolved_gaps": [
                        {
                            "question_id": "q_missing",
                            "claim": "The helper's failure behavior is unresolved.",
                            "how_to_check": "Read the helper body.",
                        }
                    ],
                    "followups": [
                        {
                            "question": "Inspect a lower-priority peer.",
                            "tool": "read_file",
                            "args": {
                                "path": "src/peer.ts",
                                "reason": "General exploration.",
                            },
                        },
                        {
                            "question": "Continue the deciding token-contract read.",
                            "tool": "read_file",
                            "args": {
                                "path": "src/token.ts",
                                "symbols": [
                                    "getToken",
                                    "mintToken",
                                    "REQUIRED_SCOPE",
                                ],
                                "reason": "Resolve the Route fact before general exploration.",
                            },
                        },
                    ],
                    "complete": False,
                },
                {
                    "summary": "The broadened exact-head slice was consumed.",
                    "answered": [],
                    "unresolved_gaps": [],
                    "followups": [],
                    "complete": True,
                },
            ]
        )

        from lambdas.LlamaPReviewPipeline.context_engine import initialization

        original = initialization.get_repo_structure_for_llm
        initialization.get_repo_structure_for_llm = lambda *_args, **kwargs: _repo_tree(
            "src/token.ts", "src/peer.ts"
        )
        try:
            context, meta = collect_context_pfr(
                runtime=runtime,
                github_token="token",
                repo_full_name="owner/repo",
                pr_content=_pfr_pr_content(),
                pr_details="# PR\n",
                head_sha="abcdef123456",
                default_branch="main",
                client=client,
                soft_time_budget=0.02,
            )
        finally:
            initialization.get_repo_structure_for_llm = original

        token_reads = [
            event
            for event in meta["evidence_ledger"]["evidence_events"]
            if event["tool"] == "read_file"
            and event["args"].get("path") == "src/token.ts"
        ]
        self.assertEqual(len(token_reads), 2)
        self.assertTrue(token_reads[0]["backend_attempted"])
        self.assertEqual(token_reads[-1]["outcome"], "hit")
        self.assertFalse(token_reads[-1]["backend_attempted"])
        self.assertIn("mintToken", token_reads[-1]["args"]["symbols"])
        self.assertNotIn("src/token.ts", meta["budget_skipped_verification_paths"])
        self.assertIn("src/peer.ts", meta["budget_skipped_verification_paths"])
        self.assertIn("mintToken", context)

    def test_soft_budget_read_rescue_requires_new_evidence_scope(self):
        state = SimpleNamespace(
            read_success_paths={"src/token.ts"},
            source_text_cache={"src/token.ts": {"content": "token source"}},
            tool_events=[
                {
                    "tool": "read_file",
                    "outcome": "hit",
                    "args": {
                        "path": "src/token.ts",
                        "mode": "content",
                        "symbols": ["getToken", "REQUIRED_SCOPE"],
                    },
                    "metadata": {
                        "coverage_type": "file_slice",
                        "backend_full_file_fetched": True,
                    },
                }
            ],
        )

        self.assertFalse(
            _read_step_can_expand_evidence(
                state,
                {
                    "path": "src/token.ts",
                    "mode": "content",
                    "symbols": ["getToken"],
                },
            )
        )
        self.assertTrue(
            _read_step_can_expand_evidence(
                state,
                {
                    "path": "src/token.ts",
                    "mode": "content",
                    "symbols": ["getToken", "mintToken"],
                },
            )
        )
        self.assertTrue(
            _read_step_can_expand_evidence(
                state,
                {"path": "src/token.ts", "mode": "content"},
            )
        )

        state.tool_events[0]["metadata"]["coverage_type"] = "full_file"
        self.assertFalse(
            _read_step_can_expand_evidence(
                state,
                {
                    "path": "src/token.ts",
                    "mode": "content",
                    "symbols": ["mintToken"],
                },
            )
        )

    def test_pfr_complete_no_hit_keeps_first_reconcile_terminal(self):
        runtime = _Runtime()
        client = _FakePfrClient(
            [
                {
                    "complexity": "normal",
                    "pr_type": "code",
                    "risk_domains": [],
                    "verification_plan": [
                        {
                            "question": "Inspect the changed service file.",
                            "why_it_matters": "It is the changed behavior.",
                            "tool": "read_file",
                            "args": {"path": "src/service.py"},
                        }
                    ],
                },
                {
                    "summary": "The planned evidence is sufficient.",
                    "answered": [],
                    "unresolved_gaps": [],
                    "followups": [],
                    "complete": True,
                },
            ]
        )

        from lambdas.LlamaPReviewPipeline.context_engine import initialization

        with patch.object(
            initialization,
            "get_repo_structure_for_llm",
            return_value=_repo_tree("src/service.py"),
        ), patch(
            "lambdas.LlamaPReviewPipeline.context_engine.pfr.evidence_execution._safety_sweep",
            return_value=0,
        ) as sweep:
            _context, meta = collect_context_pfr(
                runtime=runtime,
                github_token="token",
                repo_full_name="owner/repo",
                pr_content=_pfr_pr_content(),
                pr_details="# PR\n",
                head_sha="abcdef123456",
                default_branch="main",
                client=client,
            )

        self.assertEqual(
            [call["trace_phase"] for call in client.calls],
            ["pfr_plan", "pfr_reconcile"],
        )
        sweep.assert_called_once()
        self.assertEqual(meta["pfr_terminal_reconcile_round"], 1)
        self.assertEqual(
            meta["pfr_terminal_reconcile_trigger"],
            "initial_complete_no_new_hit",
        )
        self.assertEqual(meta["pfr_sweep_hit_count"], 0)
        self.assertEqual(meta["pfr_post_terminal_tool_call_count"], 0)

    def test_pfr_sweep_hit_gets_one_terminal_reconcile_and_no_third_round(self):
        runtime = _Runtime()
        terminal_followup = {
            "question": "Inspect a possible third-round contract.",
            "tool": "read_file",
            "args": {"path": "src/terminal.py"},
        }
        client = _FakePfrClient(
            [
                {
                    "complexity": "normal",
                    "pr_type": "code",
                    "risk_domains": [],
                    "verification_plan": [
                        {
                            "question": "Inspect the changed service file.",
                            "why_it_matters": "It is the changed behavior.",
                            "tool": "read_file",
                            "args": {"path": "src/service.py"},
                        }
                    ],
                },
                {
                    "summary": "The planned evidence was sufficient before the sweep.",
                    "answered": [],
                    "unresolved_gaps": [],
                    "followups": [],
                    "complete": True,
                },
                {
                    "summary": "The sweep evidence was consumed; one request remains diagnostic only.",
                    "answered": [],
                    "unresolved_gaps": [],
                    "followups": [terminal_followup],
                    "complete": False,
                },
            ]
        )

        from lambdas.LlamaPReviewPipeline.context_engine import initialization

        with patch.object(
            initialization,
            "get_repo_structure_for_llm",
            return_value=_repo_tree("src/service.py", "src/terminal.py"),
        ), patch(
            "lambdas.LlamaPReviewPipeline.context_engine.pfr.evidence_execution._safety_sweep",
            return_value=1,
        ) as sweep:
            _context, meta = collect_context_pfr(
                runtime=runtime,
                github_token="token",
                repo_full_name="owner/repo",
                pr_content=_pfr_pr_content(),
                pr_details="# PR\n",
                head_sha="abcdef123456",
                default_branch="main",
                client=client,
            )

        self.assertEqual(
            [call["trace_phase"] for call in client.calls],
            ["pfr_plan", "pfr_reconcile", "pfr_reconcile"],
        )
        sweep.assert_called_once()
        self.assertEqual(meta["pfr_terminal_reconcile_round"], 2)
        self.assertEqual(meta["pfr_terminal_reconcile_trigger"], "sweep_hit")
        self.assertEqual(meta["pfr_sweep_hit_count"], 1)
        self.assertEqual(meta["pfr_post_terminal_tool_call_count"], 0)
        self.assertEqual(len(meta["terminal_unexecuted_followups"]), 1)
        terminal = meta["terminal_unexecuted_followups"][0]
        self.assertEqual(terminal["reason"], "terminal_round_reached")
        self.assertEqual(terminal["path"], "src/terminal.py")
        self.assertFalse(any(path == "src/terminal.py" for _repo, path, _sha in runtime.read_calls))

    def test_pfr_second_reconcile_gets_one_bounded_terminal_evidence_read(self):
        runtime = _Runtime()
        terminal_question = "Inspect the terminal consumer."
        terminal_question_args = {
            "path": "src/terminal.py",
            "mode": "content",
            "symbols": ["run"],
        }
        terminal_question_id = stable_id(
            "q",
            {
                "question": terminal_question.lower(),
                "tool": "read_file",
                "args": terminal_question_args,
            },
        )
        client = _FakePfrClient(
            [
                {
                    "complexity": "normal",
                    "pr_type": "code",
                    "risk_domains": [],
                    "verification_plan": [
                        {
                            "question": "Inspect the changed service file.",
                            "why_it_matters": "It is the changed behavior.",
                            "tool": "read_file",
                            "args": {"path": "src/service.py"},
                        }
                    ],
                }
            ]
        )
        reconciles = [
            {
                "summary": "One caller still needs inspection.",
                "answered": [],
                "unresolved_gaps": [],
                "followups": [
                    {
                        "question": terminal_question,
                        "tool": "read_file",
                        "args": terminal_question_args,
                    }
                ],
                "complete": False,
            },
            {
                "summary": "The terminal consumer still needs inspection.",
                "answered": [],
                "unresolved_gaps": [
                    {
                        "claim": "Could not verify whether the terminal consumer accepts the changed shape.",
                        "how_to_check": "Read the terminal consumer.",
                        "question_id": terminal_question_id,
                    }
                ],
                "followups": [
                    {
                        "question": "Inspect the terminal consumer.",
                        "tool": "read_file",
                        "args": {
                            "path": "src/terminal.py",
                            "mode": "content",
                        },
                    }
                ],
                "complete": False,
            },
        ]

        from lambdas.LlamaPReviewPipeline.context_engine import initialization

        with patch.object(
            initialization,
            "get_repo_structure_for_llm",
            return_value=_repo_tree(
                "src/service.py",
                "src/caller.py",
                "src/terminal.py",
            ),
        ), patch(
            "lambdas.LlamaPReviewPipeline.context_engine.pfr.reconcile_contract._reconcile",
            side_effect=reconciles,
        ) as reconcile:
            _context, meta = collect_context_pfr(
                runtime=runtime,
                github_token="token",
                repo_full_name="owner/repo",
                pr_content=_pfr_pr_content(),
                pr_details="# PR\n",
                head_sha="abcdef123456",
                default_branch="main",
                client=client,
            )

        self.assertEqual(reconcile.call_count, 2)
        self.assertTrue(
            any(
                path == "src/terminal.py"
                for _repo, path, _sha in runtime.read_calls
            )
        )
        self.assertEqual(meta["pfr_terminal_evidence_read_count"], 1)
        self.assertEqual(meta["pfr_terminal_evidence_outcome"], "hit")
        self.assertFalse(
            meta["pfr_terminal_reconcile_covers_all_evidence"]
        )
        self.assertTrue(meta["pfr_direct_evidence_only"])
        self.assertEqual(
            meta["pfr_terminal_reconcile_trigger"],
            "terminal_evidence_read",
        )
        self.assertEqual(meta["pfr_post_terminal_tool_call_count"], 0)
        self.assertEqual(meta["terminal_unexecuted_followups"], [])
        self.assertEqual(
            len(meta["pfr_reconcile"]["unresolved_gaps"]),
            1,
        )
        terminal_resolution_id = meta["pfr_reconcile"]["unresolved_gaps"][0][
            "resolution_id"
        ]
        terminal_resolution = next(
            item
            for item in meta["evidence_ledger"]["resolutions"]
            if item["id"] == terminal_resolution_id
        )
        terminal_events = [
            item
            for item in meta["evidence_ledger"]["evidence_events"]
            if item.get("coverage_type") == "full_file"
            and "src/terminal.py" in (item.get("paths") or [])
        ]
        self.assertEqual(len(terminal_events), 1)
        self.assertNotEqual(
            terminal_events[0]["question_id"],
            terminal_resolution["question_id"],
        )
        self.assertFalse(terminal_events[0]["backend_attempted"])

    def test_pfr_terminal_evidence_never_crosses_hard_read_cap(self):
        runtime = _Runtime()
        prior_question = "Inspect the first caller."
        prior_args = {"path": "src/caller.py", "mode": "content"}
        prior_question_id = stable_id(
            "q",
            {
                "question": prior_question.lower(),
                "tool": "read_file",
                "args": prior_args,
            },
        )
        client = _FakePfrClient(
            [
                {
                    "complexity": "normal",
                    "pr_type": "code",
                    "risk_domains": [],
                    "verification_plan": [
                        {
                            "question": "Inspect the changed service file.",
                            "why_it_matters": "It is the changed behavior.",
                            "tool": "read_file",
                            "args": {"path": "src/service.py"},
                        }
                    ],
                }
            ]
        )
        reconciles = [
            {
                "summary": "Read one caller.",
                "answered": [],
                "unresolved_gaps": [],
                "followups": [
                    {
                        "question": prior_question,
                        "tool": "read_file",
                        "args": prior_args,
                    }
                ],
                "complete": False,
            },
            {
                "summary": "One terminal read remains.",
                "answered": [],
                "unresolved_gaps": [
                    {
                        "claim": "Could not verify the final consumer.",
                        "how_to_check": "Read the final consumer.",
                        "question_id": prior_question_id,
                    }
                ],
                "followups": [
                    {
                        "question": "Inspect the final consumer.",
                        "tool": "read_file",
                        "args": {"path": "src/caller.py"},
                    }
                ],
                "complete": False,
            },
        ]

        from lambdas.LlamaPReviewPipeline.context_engine import initialization

        with patch.object(
            initialization,
            "get_repo_structure_for_llm",
            return_value=_repo_tree(
                "src/service.py",
                "src/caller.py",
                "src/terminal.py",
            ),
        ), patch(
            "lambdas.LlamaPReviewPipeline.context_engine.pfr.reconcile_contract._reconcile",
            side_effect=reconciles,
        ):
            _context, meta = collect_context_pfr(
                runtime=runtime,
                github_token="token",
                repo_full_name="owner/repo",
                pr_content=_pfr_pr_content(),
                pr_details="# PR\n",
                head_sha="abcdef123456",
                default_branch="main",
                client=client,
                max_read_calls=2,
            )

        self.assertEqual(
            sum(
                1
                for _repo, path, _sha in runtime.read_calls
                if path == "src/caller.py"
            ),
            1,
        )
        self.assertEqual(meta["pfr_terminal_evidence_read_count"], 0)
        self.assertEqual(
            meta["pfr_terminal_evidence_outcome"],
            "hard_read_cap_reached",
        )
        self.assertTrue(
            meta["pfr_terminal_reconcile_covers_all_evidence"]
        )
        self.assertEqual(meta["pfr_post_terminal_tool_call_count"], 0)

    def test_terminal_evidence_requires_an_incomplete_gap_and_content_read(self):
        from types import SimpleNamespace

        question_id = "q_existing"
        state = SimpleNamespace(
            read_calls=0,
            max_read_calls=6,
            remaining_time=lambda: 120,
            deadline=None,
            tool_events=[],
            repo_inventory=None,
            evidence_ledger=EvidenceLedger(expected_head_sha="head"),
        )
        executor = SimpleNamespace(state=state, execute=lambda *_args, **_kwargs: None)
        cases = [
            (
                {
                    "complete": True,
                    "unresolved_gaps": [
                        {
                            "claim": "Caller behavior remains unresolved.",
                            "question_id": question_id,
                        }
                    ],
                },
                [{"tool": "read_file", "args": {"path": "src/a.py"}}],
                "reconcile_complete",
            ),
            (
                {
                    "complete": False,
                    "unresolved_gaps": [],
                },
                [{"tool": "read_file", "args": {"path": "src/a.py"}}],
                "no_unresolved_gap",
            ),
            (
                {
                    "complete": False,
                    "unresolved_gaps": [
                        {
                            "claim": "Caller behavior remains unresolved.",
                            "question_id": question_id,
                        }
                    ],
                },
                [{"tool": "search_code", "args": {"query": "Caller"}}],
                "no_single_content_read",
            ),
        ]
        for reconcile, followups, expected in cases:
            with self.subTest(expected=expected):
                selected, outcome = _terminal_evidence_read(
                    executor,
                    reconcile,
                    followups,
                )
                self.assertIsNone(selected)
                self.assertEqual(outcome, expected)

    def test_terminal_evidence_requires_one_content_read_and_respects_deadline_reserve(self):
        from types import SimpleNamespace

        question_id = "q_first"
        reconcile = {
            "complete": False,
            "unresolved_gaps": [
                {
                    "claim": "The first-file behavior remains unresolved.",
                    "question_id": question_id,
                }
            ],
        }
        followups = [
            {
                "tool": "read_file",
                "question": "Inspect the first file.",
                "args": {"path": "src/first.py", "mode": "content"},
            },
        ]
        state = SimpleNamespace(
            read_calls=0,
            max_read_calls=6,
            remaining_time=lambda: 120,
            deadline=SimpleNamespace(remaining_seconds=lambda: 120),
            tool_events=[],
            repo_inventory=None,
            soft_budget_exhausted=True,
            evidence_ledger=EvidenceLedger(expected_head_sha="head"),
        )
        def execute_selected(_tool_call, *, question_id):
            self.assertTrue(question_id.startswith("q_"))
            state.tool_events.append({"outcome": "hit"})

        executor = SimpleNamespace(state=state, execute=execute_selected)
        selected, outcome = _terminal_evidence_read(
            executor,
            reconcile,
            followups,
        )
        self.assertIs(selected, followups[0])
        self.assertEqual(outcome, "hit")

        state.deadline = SimpleNamespace(remaining_seconds=lambda: 29)
        selected, outcome = _terminal_evidence_read(
            executor,
            reconcile,
            followups,
        )
        self.assertIsNone(selected)
        self.assertEqual(outcome, "pipeline_deadline_reserve")

    def test_terminal_evidence_refuses_ambiguous_reads(self):
        from types import SimpleNamespace

        state = SimpleNamespace(
            read_calls=0,
            max_read_calls=6,
            remaining_time=lambda: 120,
            deadline=SimpleNamespace(remaining_seconds=lambda: 120),
            tool_events=[],
            repo_inventory=None,
            evidence_ledger=EvidenceLedger(expected_head_sha="head"),
        )
        executor = SimpleNamespace(
            state=state,
            execute=lambda *_args, **_kwargs: self.fail(
                "ambiguous terminal closure must not execute"
            ),
        )
        reconcile = {
            "complete": False,
            "unresolved_gaps": [
                {"claim": "First gap.", "question_id": "q_one"},
                {"claim": "Second gap.", "question_id": "q_two"},
            ],
        }

        selected, outcome = _terminal_evidence_read(
            executor,
            reconcile,
            [
                {
                    "tool": "read_file",
                    "args": {
                        "path": "src/target.py",
                        "mode": "content",
                    },
                },
                {
                    "tool": "read_file",
                    "args": {
                        "path": "src/other.py",
                        "mode": "content",
                    },
                },
            ],
        )
        self.assertIsNone(selected)
        self.assertEqual(outcome, "no_single_content_read")

        reconcile["unresolved_gaps"] = []
        selected, outcome = _terminal_evidence_read(
            executor,
            reconcile,
            [
                {
                    "tool": "read_file",
                    "args": {
                        "path": "src/target.py",
                        "mode": "content",
                        "symbols": ["Unrelated"],
                    },
                }
            ],
        )
        self.assertIsNone(selected)
        self.assertEqual(outcome, "no_unresolved_gap")

    def test_terminal_evidence_executes_one_read_under_fresh_question(self):
        """A late-discovered file must not seal the boundary unread."""

        from types import SimpleNamespace

        executed = {}

        def execute_selected(_tool_call, *, question_id):
            executed["question_id"] = question_id
            state.tool_events.append({"outcome": "hit"})

        ledger = EvidenceLedger(expected_head_sha="head")
        prior_question_id = ledger.register_question(
            question="Where is the config discovered?",
            tool="search_code",
            args={"query": "vite.config", "reason": "Find config."},
        )
        state = SimpleNamespace(
            read_calls=0,
            max_read_calls=6,
            remaining_time=lambda: 120,
            deadline=SimpleNamespace(remaining_seconds=lambda: 120),
            tool_events=[],
            repo_inventory=None,
            evidence_ledger=ledger,
        )
        executor = SimpleNamespace(state=state, execute=execute_selected)
        reconcile = {
            "complete": False,
            "unresolved_gaps": [
                {
                    "claim": "The discovered config remains unresolved.",
                    "question_id": prior_question_id,
                }
            ],
        }
        followups = [
            {
                "tool": "read_file",
                "question": "Inspect the discovered config.",
                "args": {"path": "vite.config.ts", "mode": "content"},
            }
        ]

        selected, outcome = _terminal_evidence_read(executor, reconcile, followups)

        self.assertIs(selected, followups[0])
        self.assertEqual(outcome, "hit")
        self.assertNotEqual(executed["question_id"], prior_question_id)
        self.assertIn(executed["question_id"], ledger.questions)

    def test_terminal_evidence_floor_stays_unambiguous(self):
        from types import SimpleNamespace

        state = SimpleNamespace(
            read_calls=0,
            max_read_calls=6,
            remaining_time=lambda: 120,
            deadline=SimpleNamespace(remaining_seconds=lambda: 120),
            tool_events=[],
            repo_inventory=None,
            evidence_ledger=EvidenceLedger(expected_head_sha="head"),
        )
        executor = SimpleNamespace(
            state=state,
            execute=lambda *_args, **_kwargs: self.fail(
                "ambiguous terminal closure must not execute"
            ),
        )
        reconcile = {
            "complete": False,
            "unresolved_gaps": [
                {
                    "claim": "The discovery result remains unresolved.",
                    "question_id": "q_discovery",
                }
            ],
        }

        selected, outcome = _terminal_evidence_read(
            executor,
            reconcile,
            [
                {
                    "tool": "read_file",
                    "args": {"path": "vite.config.ts", "mode": "content"},
                },
                {
                    "tool": "read_file",
                    "args": {"path": "rollup.config.ts", "mode": "content"},
                },
            ],
        )
        self.assertIsNone(selected)
        self.assertEqual(outcome, "no_single_content_read")

    def test_terminal_evidence_skips_an_unaddressable_large_read(self):
        from types import SimpleNamespace

        reconcile = {
            "complete": False,
            "unresolved_gaps": [
                {
                    "claim": "The small-file behavior remains unresolved.",
                    "question_id": "q_small",
                }
            ],
        }
        followups = [
            {
                "tool": "read_file",
                "question": "Inspect the large file.",
                "args": {"path": "src/large.py", "mode": "content"},
            },
            {
                "tool": "read_file",
                "question": "Inspect the small file.",
                "args": {"path": "src/small.py", "mode": "content"},
            },
        ]
        sizes = {"src/large.py": 200_000, "src/small.py": 2_000}
        state = SimpleNamespace(
            read_calls=0,
            max_read_calls=6,
            remaining_time=lambda: 120,
            deadline=SimpleNamespace(remaining_seconds=lambda: 120),
            tool_events=[],
            repo_inventory=SimpleNamespace(
                file_size_bytes=lambda path: sizes[path]
            ),
            evidence_ledger=EvidenceLedger(expected_head_sha="head"),
        )
        def execute_selected(_tool_call, *, question_id):
            self.assertTrue(question_id.startswith("q_"))
            state.tool_events.append({"outcome": "hit"})

        executor = SimpleNamespace(state=state, execute=execute_selected)
        selected, outcome = _terminal_evidence_read(
            executor,
            reconcile,
            followups,
        )

        self.assertIs(selected, followups[1])
        self.assertEqual(outcome, "hit")

    def test_pfr_single_round_mode_executes_no_post_reconcile_tools(self):
        runtime = _Runtime()
        client = _FakePfrClient(
            [
                {
                    "complexity": "normal",
                    "pr_type": "code",
                    "risk_domains": [],
                    "verification_plan": [
                        {
                            "question": "Inspect the changed service file.",
                            "why_it_matters": "It is the changed behavior.",
                            "tool": "read_file",
                            "args": {"path": "src/service.py"},
                        }
                    ],
                },
                {
                    "summary": "A follow-up would be useful but no judgment round remains.",
                    "answered": [],
                    "unresolved_gaps": [],
                    "followups": [
                        {
                            "question": "Inspect the deferred contract.",
                            "tool": "read_file",
                            "args": {"path": "src/terminal.py"},
                        }
                    ],
                    "complete": False,
                },
            ]
        )

        from lambdas.LlamaPReviewPipeline.context_engine import initialization
        from lambdas.LlamaPReviewPipeline import config as pipeline_config

        with patch.object(
            initialization,
            "get_repo_structure_for_llm",
            return_value=_repo_tree("src/service.py", "src/terminal.py"),
        ), patch.object(
            pipeline_config,
            "PFR_MAX_RECONCILE_ROUNDS",
            1,
        ), patch(
            "lambdas.LlamaPReviewPipeline.context_engine.pfr.evidence_execution._safety_sweep"
        ) as sweep:
            _context, meta = collect_context_pfr(
                runtime=runtime,
                github_token="token",
                repo_full_name="owner/repo",
                pr_content=_pfr_pr_content(),
                pr_details="# PR\n",
                head_sha="abcdef123456",
                default_branch="main",
                client=client,
            )

        self.assertEqual(
            [call["trace_phase"] for call in client.calls],
            ["pfr_plan", "pfr_reconcile"],
        )
        sweep.assert_not_called()
        self.assertEqual(meta["pfr_terminal_reconcile_round"], 1)
        self.assertEqual(meta["pfr_terminal_reconcile_trigger"], "round_cap_or_failure")
        self.assertEqual(meta["pfr_post_terminal_tool_call_count"], 0)
        self.assertEqual(len(meta["terminal_unexecuted_followups"]), 1)
        self.assertFalse(any(path == "src/terminal.py" for _repo, path, _sha in runtime.read_calls))

    def test_pfr_ignores_plan_attempt_to_revise_route_pr_type(self):
        runtime = _Runtime()
        client = _FakePfrClient(
            [
                {
                    "complexity": "normal",
                    "pr_type": "feature",
                    "risk_domains": ["ui"],
                    "verification_plan": [
                        {
                            "question": "Inspect the changed service file.",
                            "why_it_matters": "Changed code remains the review floor.",
                            "tool": "read_file",
                            "args": {"path": "src/service.py"},
                        }
                    ],
                },
                {"summary": "The file was checked.", "answered": [], "unresolved_gaps": [], "followups": [], "complete": True},
            ]
        )

        from lambdas.LlamaPReviewPipeline.context_engine import initialization

        original = initialization.get_repo_structure_for_llm
        initialization.get_repo_structure_for_llm = lambda *_args, **kwargs: _repo_tree("src/service.py")
        try:
            _context, meta = collect_context_pfr(
                runtime=runtime,
                github_token="token",
                repo_full_name="owner/repo",
                pr_content=_pfr_pr_content(),
                pr_details="# PR\n",
                head_sha="abcdef123456",
                default_branch="main",
                client=client,
                route_plan={
                    "complexity": "normal",
                    "pr_type": "code",
                    "risk_domains": ["ui"],
                },
            )
        finally:
            initialization.get_repo_structure_for_llm = original

        self.assertEqual(meta["pfr_plan"]["pr_type"], "code")
        self.assertNotIn("pr_type:feature->code", meta["pfr_plan_schema_warnings"])
        self.assertEqual(meta["fetch_health"]["pr_type"], "code")

    def test_pfr_applies_query_postprocess_and_records_plan_meta(self):
        runtime = _Runtime()
        client = _FakePfrClient(
            [
                {
                    "complexity": "normal",
                    "pr_type": "code",
                    "risk_domains": ["api"],
                    "verification_plan": [
                        {
                            "question": "Find worker usage.",
                            "why_it_matters": "Caller coverage matters.",
                            "tool": "search_code",
                            "args": {"query": "class Worker", "reason": "Find worker usage.", "intent": "external_usage"},
                        }
                    ],
                },
                {"summary": "Searches were checked.", "answered": [], "unresolved_gaps": [], "followups": [], "complete": True},
            ]
        )

        from lambdas.LlamaPReviewPipeline.context_engine import initialization

        original = initialization.get_repo_structure_for_llm
        initialization.get_repo_structure_for_llm = lambda *_args, **kwargs: _repo_tree("src/service.py")
        try:
            _context, meta = collect_context_pfr(
                runtime=runtime,
                github_token="token",
                repo_full_name="owner/repo",
                pr_content=_pfr_pr_content(),
                pr_details="# PR\n",
                head_sha="abcdef123456",
                default_branch="main",
                client=client,
                max_search_calls=4,
            )
        finally:
            initialization.get_repo_structure_for_llm = original

        self.assertIn(
            "add_model:class Worker",
            meta["pfr_query_postprocess"],
        )
        self.assertIn("add_removed_reserved:run", meta["pfr_query_postprocess"])
        self.assertFalse(
            any("variant" in item for item in meta["pfr_query_postprocess"])
        )
        planned_searches = [item for item in meta["pfr_plan"]["verification_plan"] if item["tool"] == "search_code"]
        self.assertTrue(any(item["args"].get("intent") == "external_usage" for item in planned_searches))
        questions_by_query = {
            item["args"]["query"]: item["question"] for item in planned_searches
        }
        self.assertEqual(
            questions_by_query["class Worker"],
            "Find worker usage.",
        )
        self.assertEqual(
            questions_by_query["run"],
            "Find references to removed symbol run.",
        )

    def test_pfr_does_not_rebind_dropped_model_questions_to_diff_search_seeds(self):
        runtime = _Runtime()
        original_questions = [
            "Do the shim methods exist and delegate to the extracted service?",
            "Are the removed reachability fields no longer referenced?",
            "Were the status helpers removed without leaving call sites?",
        ]
        client = _FakePfrClient(
            [
                {
                    "complexity": "normal",
                    "pr_type": "code",
                    "risk_domains": [],
                    "verification_plan": [
                        {
                            "question": original_questions[0],
                            "why_it_matters": "Confirm the compatibility shim contract.",
                            "tool": "search_code",
                            "args": {
                                "query": "void CheckReachabilityAsync|void WakeServer|void DiagnoseServer",
                                "reason": "Find the compatibility shim methods.",
                                "intent": "external_usage",
                            },
                        },
                        {
                            "question": original_questions[1],
                            "why_it_matters": "Confirm removed state is not still referenced.",
                            "tool": "search_code",
                            "args": {
                                "query": "_reachTimer|_reachBusy|_latencySamples",
                                "reason": "Find references to removed reachability state.",
                                "intent": "removal_cleanup",
                            },
                        },
                        {
                            "question": original_questions[2],
                            "why_it_matters": "Confirm status-helper cleanup.",
                            "tool": "search_code",
                            "args": {
                                "query": "SetStatus|StatusBrush",
                                "reason": "Find references to removed status helpers.",
                                "intent": "removal_cleanup",
                            },
                        },
                    ],
                },
                {
                    "summary": "Diff-derived removal searches were executed.",
                    "answered": [],
                    "unresolved_gaps": [],
                    "followups": [],
                    "complete": True,
                },
            ]
        )
        pr_content = {
            "pr_metadata": {"number": 158, "title": "Extract reachability service"},
            "file_changes": [
                {
                    "file_path": "MainWindow.xaml.cs",
                    "change_type": "modified",
                    "diff": (
                        "@@ -1,5 +1 @@\n"
                        "-private async Task CheckReachabilityAsync() { }\n"
                        "-private void WakeServer() { }\n"
                        "-private void DiagnoseServer() { }\n"
                        "-private void SetStatus() { }\n"
                        "-private Brush StatusBrush() { }\n"
                        "+private void Window_Loaded() { }\n"
                    ),
                }
            ],
        }

        from lambdas.LlamaPReviewPipeline.context_engine import initialization

        original = initialization.get_repo_structure_for_llm
        initialization.get_repo_structure_for_llm = lambda *_args, **kwargs: _repo_tree(
            "MainWindow.xaml.cs"
        )
        try:
            _context, meta = collect_context_pfr(
                runtime=runtime,
                github_token="token",
                repo_full_name="owner/repo",
                pr_content=pr_content,
                pr_details="# PR\n",
                head_sha="abcdef123456",
                default_branch="main",
                client=client,
                max_search_calls=5,
            )
        finally:
            initialization.get_repo_structure_for_llm = original

        planned_searches = [
            item
            for item in meta["pfr_plan"]["verification_plan"]
            if item["tool"] == "search_code"
        ]
        possible_queries = {
            "CheckReachabilityAsync",
            "DiagnoseServer",
            "SetStatus",
            "StatusBrush",
            "WakeServer",
        }
        actual_queries = {item["args"]["query"] for item in planned_searches}
        self.assertEqual(len(actual_queries), 5)
        self.assertTrue(actual_queries.issubset(possible_queries))
        self.assertEqual(
            {item["question"] for item in planned_searches},
            {
                f"Find references to removed symbol {query}."
                for query in actual_queries
            },
        )

        ledger = meta["evidence_ledger"]
        questions_by_text = {item["text"]: item for item in ledger["questions"]}
        resolutions_by_question = {
            item["question_id"]: item for item in ledger["resolutions"]
        }
        for question in original_questions:
            ledger_question = questions_by_text[question]
            self.assertEqual(ledger_question["lifecycle"], "dropped_invalid")
            self.assertEqual(ledger_question["event_ids"], [])
            self.assertEqual(
                resolutions_by_question[ledger_question["id"]]["status"], "unknown"
            )

    def test_pfr_duplicate_normalized_query_answer_prefers_executed_question(self):
        class WorkerRuntime(_Runtime):
            def search_code(self, query, repo):
                self.search_calls.append((query, repo))
                return [
                    {
                        "index": 0,
                        "path": "src/service.py",
                        "content": "class Worker:\n    pass\n",
                    }
                ]

            def get_file_content(self, repo, path, *, sha=None):
                self.read_calls.append((repo, path, sha))
                if path == "AGENTS.md":
                    return (
                        "Run the fast test suite before recommending merge."
                    )
                return "class Worker:\n    pass\n"

        runtime = WorkerRuntime()
        question = "Find worker usage."
        duplicate_step = {
            "question": question,
            "why_it_matters": "Caller coverage matters.",
            "tool": "search_code",
            "args": {
                "query": "class Worker",
                "reason": "Find worker usage.",
                "intent": "external_usage",
            },
        }
        client = _FakePfrClient(
            [
                {
                    "complexity": "normal",
                    "pr_type": "code",
                    "risk_domains": [],
                    "verification_plan": [dict(duplicate_step), dict(duplicate_step)],
                },
                {
                    "summary": "Worker usage was found.",
                    "answered": [
                        {
                            "question": question,
                            "evidence": "src/service.py contains a Worker usage.",
                        }
                    ],
                    "unresolved_gaps": [],
                    "followups": [],
                    "complete": True,
                },
            ]
        )
        pr_content = {
            "pr_metadata": {"number": 159, "title": "Update worker"},
            "file_changes": [
                {
                    "file_path": "src/service.py",
                    "change_type": "modified",
                    "diff": "@@ -1 +1 @@\n-def old(): pass\n+def new(): pass\n",
                }
            ],
        }

        from lambdas.LlamaPReviewPipeline.context_engine import initialization

        original = initialization.get_repo_structure_for_llm
        initialization.get_repo_structure_for_llm = lambda *_args, **kwargs: _repo_tree(
            "src/service.py"
        )
        try:
            _context, meta = collect_context_pfr(
                runtime=runtime,
                github_token="token",
                repo_full_name="owner/repo",
                pr_content=pr_content,
                pr_details="# PR\n",
                head_sha="abcdef123456",
                default_branch="main",
                client=client,
                max_search_calls=3,
            )
        finally:
            initialization.get_repo_structure_for_llm = original

        executed_question = next(
            item
            for item in meta["evidence_ledger"]["questions"]
            if item["text"] == question and item["lifecycle"] == "executed"
        )
        self.assertEqual(
            [
                item
                for item in meta["evidence_ledger"]["questions"]
                if item["text"] == question
            ],
            [executed_question],
        )
        self.assertEqual(len(meta["pfr_reconcile"]["answered"]), 1)
        self.assertEqual(
            meta["pfr_reconcile"]["answered"][0]["question_id"],
            executed_question["id"],
        )
        self.assertEqual(
            len(meta["pfr_reconcile"]["answered"][0]["evidence_refs"]), 1
        )
        self.assertEqual(meta["pfr_reconcile"]["unresolved_gaps"], [])

    def test_pfr_repairs_planned_read_file_target_path_and_records_meta(self):
        runtime = _Runtime()
        client = _FakePfrClient(
            [
                {
                    "complexity": "normal",
                    "pr_type": "code",
                    "risk_domains": ["api"],
                    "verification_plan": [
                        {
                            "question": "Inspect the changed service file.",
                            "why_it_matters": "It contains the changed behavior.",
                            "tool": "read_file",
                            "args": {"target_path": "src/service.py", "max_depth": 2},
                        }
                    ],
                },
                {"summary": "The file was checked.", "answered": [], "unresolved_gaps": [], "followups": [], "complete": True},
            ]
        )

        from lambdas.LlamaPReviewPipeline.context_engine import initialization

        original = initialization.get_repo_structure_for_llm
        initialization.get_repo_structure_for_llm = lambda *_args, **kwargs: _repo_tree("src/service.py")
        try:
            _context, meta = collect_context_pfr(
                runtime=runtime,
                github_token="token",
                repo_full_name="owner/repo",
                pr_content=_pfr_pr_content(),
                pr_details="# PR\n",
                head_sha="abcdef123456",
                default_branch="main",
                client=client,
            )
        finally:
            initialization.get_repo_structure_for_llm = original

        self.assertIn(("owner/repo", "src/service.py", "abcdef123456"), runtime.read_calls)
        self.assertEqual(meta["tool_arg_repair_counts"]["read_file.path_from_target_path"], 1)
        self.assertEqual(meta["tool_arg_repair_counts"]["read_file.dropped_max_depth"], 1)
        read_step = next(
            step
            for step in meta["pfr_plan"]["verification_plan"]
            if step["tool"] == "read_file"
        )
        self.assertEqual(read_step["args"]["path"], "src/service.py")
        self.assertEqual(meta["fetch_health"]["status"], "healthy")
        self.assertEqual(
            meta["fetch_health"]["planned_retrieval_status"], "complete"
        )

    def test_pfr_skips_nonexistent_planned_reads_without_tool_error(self):
        runtime = _Runtime()
        client = _FakePfrClient(
            [
                {
                    "complexity": "normal",
                    "pr_type": "code",
                    "risk_domains": ["api"],
                    "verification_plan": [
                        {
                            "question": "Inspect the changed service file.",
                            "why_it_matters": "It contains the changed behavior.",
                            "tool": "read_file",
                            "args": {"path": "src/service.py"},
                        },
                        {
                            "question": "Inspect a related missing file.",
                            "why_it_matters": "It could contain related wiring.",
                            "tool": "read_file",
                            "args": {"path": "src/missing.py"},
                        },
                    ],
                },
                {"summary": "One file was checked and one related read failed.", "answered": [], "unresolved_gaps": [], "followups": [], "complete": True},
            ]
        )

        from lambdas.LlamaPReviewPipeline.context_engine import initialization

        original = initialization.get_repo_structure_for_llm
        initialization.get_repo_structure_for_llm = lambda *_args, **kwargs: _repo_tree("src/service.py")
        try:
            _context, meta = collect_context_pfr(
                runtime=runtime,
                github_token="token",
                repo_full_name="owner/repo",
                pr_content=_pfr_pr_content(),
                pr_details="# PR\n",
                head_sha="abcdef123456",
                default_branch="main",
                client=client,
            )
        finally:
            initialization.get_repo_structure_for_llm = original

        self.assertEqual(
            meta["fetch_health"]["status"], "partial_or_failed_context"
        )
        self.assertEqual(
            meta["fetch_health"]["planned_retrieval_status"], "degraded"
        )
        self.assertIn("invalid_plan", meta["fetch_health"]["reasons"])
        self.assertEqual(meta["fetch_health"]["planned_read_paths"], ["src/service.py", "src/missing.py"])
        self.assertEqual(meta["fetch_health"]["planned_unread_paths"], ["src/missing.py"])
        self.assertEqual(meta["planned_unread_paths"], ["src/missing.py"])
        self.assertEqual(meta["planned_invalid_read_paths"], ["src/missing.py"])
        self.assertEqual(meta["read_error_paths"], [])
        self.assertFalse(any(call[1] == "src/missing.py" for call in runtime.read_calls))

    def test_pfr_preserves_model_authored_unknown_for_deep_judgment(self):
        runtime = _Runtime()
        client = _FakePfrClient(
            [
                {
                    "complexity": "normal",
                    "pr_type": "code",
                    "risk_domains": ["build"],
                    "verification_plan": [
                        {
                            "question": "Inspect the changed service file.",
                            "why_it_matters": "It contains the changed behavior.",
                            "tool": "read_file",
                            "args": {"path": "src/service.py"},
                        }
                    ],
                },
                {
                    "summary": "Spring Boot version 4.1.0 may not exist and may break the build.",
                    "answered": [{"question": "Inspect versions.", "evidence": "Java version has no such release."}],
                    "unresolved_gaps": [
                        {
                            "claim": "Spring Boot version 4.1.0 may not exist and may break the build.",
                            "how_to_check": "As of 2025 this version does not exist; the latest stable is 3.4.x. Run the build.",
                        }
                    ],
                    "followups": [],
                    "complete": True,
                },
            ]
        )

        from lambdas.LlamaPReviewPipeline.context_engine import initialization

        original = initialization.get_repo_structure_for_llm
        initialization.get_repo_structure_for_llm = lambda *_args, **kwargs: _repo_tree("src/service.py")
        try:
            context, meta = collect_context_pfr(
                runtime=runtime,
                github_token="token",
                repo_full_name="owner/repo",
                pr_content=_pfr_pr_content(),
                pr_details="# PR\n",
                head_sha="abcdef123456",
                default_branch="main",
                client=client,
            )
        finally:
            initialization.get_repo_structure_for_llm = original

        joined_gaps = "; ".join(meta["known_gaps"]).lower()
        self.assertIn("spring boot version 4.1.0 may not exist", joined_gaps)
        self.assertIn("may break the build", context.lower())
        self.assertIn("as of 2025", context.lower())
        self.assertEqual(
            meta["pfr_reconcile"]["unresolved_gaps"][0]["how_to_check"],
            "As of 2025 this version does not exist; the latest stable is 3.4.x. Run the build.",
        )

    def test_pfr_zero_file_fetch_marks_partial_context(self):
        runtime = _Runtime()
        client = _FakePfrClient(
            [
                {
                    "complexity": "normal",
                    "pr_type": "code",
                    "risk_domains": ["api"],
                    "verification_plan": [
                        {
                            "question": "Inspect a missing planned file arg.",
                            "why_it_matters": "It should not look healthy.",
                            "tool": "read_file",
                            "args": {"reason": "Missing path."},
                        }
                    ],
                },
                {"summary": "The model should not claim success.", "answered": [], "unresolved_gaps": [], "followups": [], "complete": True},
            ]
        )

        from lambdas.LlamaPReviewPipeline.context_engine import initialization

        original = initialization.get_repo_structure_for_llm
        initialization.get_repo_structure_for_llm = lambda *_args, **kwargs: _repo_tree("src/service.py")
        try:
            context, meta = collect_context_pfr(
                runtime=runtime,
                github_token="token",
                repo_full_name="owner/repo",
                pr_content=_pfr_pr_content(),
                pr_details="# PR\n",
                head_sha="abcdef123456",
                default_branch="main",
                client=client,
            )
        finally:
            initialization.get_repo_structure_for_llm = original

        self.assertEqual(meta["fetch_health"]["status"], "partial_or_failed_context")
        self.assertEqual(meta["finish_reason"], "partial_or_failed_context")
        self.assertEqual(meta["read_file_missing_path_errors"], 1)
        self.assertIn("PFR retrieval was incomplete", "; ".join(meta["known_gaps"]))
        self.assertIn("PFR retrieval was incomplete", context)

    def test_pfr_model_failure_degrades_honestly_without_react_fallback(self):
        runtime = _Runtime()
        provider_body_sentinel = "PRIVATE_PROVIDER_BODY_SENTINEL"
        client = _FakePfrClient(
            fail_all=True,
            failure_exception=DeepSeekHTTPError(
                provider_body_sentinel,
                status_code=500,
            ),
        )

        from lambdas.LlamaPReviewPipeline.context_engine import initialization

        original = initialization.get_repo_structure_for_llm
        initialization.get_repo_structure_for_llm = lambda *_args, **kwargs: _repo_tree("src/service.py")
        try:
            with self.assertLogs(
                "lambdas.LlamaPReviewPipeline.context_engine.pfr.orchestration",
                level="WARNING",
            ) as captured:
                context, meta = collect_context_pfr(
                    runtime=runtime,
                    github_token="token",
                    repo_full_name="owner/repo",
                    pr_content=_pfr_pr_content(),
                    pr_details="# PR\n",
                    head_sha="abcdef123456",
                    default_branch="main",
                    client=client,
                    max_search_calls=1,
                    max_read_calls=2,
                )
        finally:
            initialization.get_repo_structure_for_llm = original

        self.assertEqual(meta["context_strategy"], "pfr")
        self.assertEqual(meta["finish_reason"], "cap_reached")
        self.assertIn("PFR plan model failed", "; ".join(meta["known_gaps"]))
        self.assertIn(
            "PFR reconcile contract failed",
            "; ".join(meta["known_gaps"]),
        )
        self.assertEqual(meta["pfr_plan_status"], "fallback_used")
        self.assertEqual(meta["pfr_plan_source"], "deterministic_fallback")
        self.assertTrue(meta["plan_fallback_used"])
        self.assertEqual(
            meta["pfr_plan_failure_kind"],
            "model_http_error",
        )
        self.assertEqual(
            meta["pfr_terminal_reconcile_failure_kind"],
            "model_http_error",
        )
        diagnostic_envelope = json.dumps(
            {
                "context": context,
                "meta": meta,
                "logs": captured.output,
            },
            default=str,
        )
        self.assertNotIn(provider_body_sentinel, diagnostic_envelope)
        self.assertIn("Known gaps", context)
        self.assertEqual(len(client.calls), 2)

    def test_paid_dispatch_ledger_failure_never_degrades_into_more_pfr_calls(self):
        record = {
            "schema_version": 2,
            "call_id": "a" * 64,
            "operation_id": "b" * 64,
            "run_id": "run-1",
            "head_sha": "abcdef123456",
            "pipeline_phase": "context",
            "pipeline_attempt": 1,
            "phase": "pfr_plan",
            "call_index": 1,
            "transport_attempt_index": 1,
            "billed_model": "deepseek-v4-flash",
        }

        from lambdas.LlamaPReviewPipeline.context_engine import initialization

        with patch.object(
            initialization,
            "get_repo_structure_for_llm",
            return_value=_repo_tree("src/service.py"),
        ):
            for fail_at, responses in (
                (
                    "plan",
                    [
                        ProviderCallLedgerError(
                            "plan ledger unavailable",
                            provider_call_record=record,
                        )
                    ],
                ),
                (
                    "reconcile",
                    [
                        {
                            "complexity": "normal",
                            "pr_type": "code",
                            "risk_domains": [],
                            "verification_plan": [
                                {
                                    "question": "Inspect the changed service file.",
                                    "why_it_matters": "It owns the changed behavior.",
                                    "tool": "read_file",
                                    "args": {"path": "src/service.py"},
                                }
                            ],
                        },
                        ProviderCallLedgerError(
                            "reconcile ledger unavailable",
                            provider_call_record={
                                **record,
                                "call_id": "c" * 64,
                                "operation_id": "d" * 64,
                                "phase": "pfr_reconcile",
                            },
                        ),
                    ],
                ),
            ):
                with self.subTest(fail_at=fail_at):
                    client = _FakePfrClient(responses)
                    with self.assertRaises(ProviderCallLedgerError):
                        collect_context_pfr(
                            runtime=_Runtime(),
                            github_token="token",
                            repo_full_name="owner/repo",
                            pr_content=_pfr_pr_content(),
                            pr_details="# PR\n",
                            head_sha="abcdef123456",
                            default_branch="main",
                            client=client,
                            max_search_calls=1,
                            max_read_calls=2,
                        )
                    self.assertEqual(
                        len(client.calls),
                        1 if fail_at == "plan" else 2,
                    )

    def test_provider_fence_failures_never_degrade_into_more_pfr_calls(self):
        record = {
            "schema_version": 2,
            "call_id": "e" * 64,
            "operation_id": "f" * 64,
            "run_id": "run-fence",
            "head_sha": "abcdef123456",
            "pipeline_phase": "context",
            "pipeline_attempt": 1,
            "phase": "pfr_plan",
            "call_index": 1,
            "transport_attempt_index": 1,
            "status": "dispatching",
            "usage_state": "unreported",
            "usage": {},
        }
        from lambdas.LlamaPReviewPipeline.context_engine import initialization

        with patch.object(
            initialization,
            "get_repo_structure_for_llm",
            return_value=_repo_tree("src/service.py"),
        ):
            for error_type in (
                ProviderCallFenceError,
                ProviderDispatchOutcomeUnknown,
            ):
                for fail_at in ("plan", "reconcile"):
                    with self.subTest(
                        error_type=error_type.__name__,
                        fail_at=fail_at,
                    ):
                        failure = error_type(
                            "provider fence failure",
                            provider_call_record={
                                **record,
                                "phase": (
                                    "pfr_plan"
                                    if fail_at == "plan"
                                    else "pfr_reconcile"
                                ),
                            },
                        )
                        responses = [failure]
                        if fail_at == "reconcile":
                            responses.insert(
                                0,
                                {
                                    "complexity": "normal",
                                    "pr_type": "code",
                                    "risk_domains": [],
                                    "verification_plan": [],
                                },
                            )
                        client = _FakePfrClient(responses)
                        with self.assertRaises(error_type):
                            collect_context_pfr(
                                runtime=_Runtime(),
                                github_token="token",
                                repo_full_name="owner/repo",
                                pr_content=_pfr_pr_content(),
                                pr_details="# PR\n",
                                head_sha="abcdef123456",
                                default_branch="main",
                                client=client,
                                max_search_calls=1,
                                max_read_calls=2,
                            )
                        self.assertEqual(
                            len(client.calls),
                            1 if fail_at == "plan" else 2,
                        )


if __name__ == "__main__":
    unittest.main()
