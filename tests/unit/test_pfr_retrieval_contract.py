import json
import unittest

from tests.unit.fakes import ensure_repo_root_on_path, set_default_env

ensure_repo_root_on_path()
set_default_env()

from lambdas.LlamaPReviewPipeline.context_engine.pfr import (
    PLAN_CONTINUATION_PROMPT,
    PLAN_PROMPT,
    RECONCILE_SYSTEM_PROMPT,
    RECONCILE_PROMPT,
    _address_large_read_steps,
    _fetch_health,
    _postprocess_planned_search_steps,
)
from lambdas.LlamaPReviewPipeline.context_engine.repo_structure import RepoInventory
from lambdas.LlamaPReviewPipeline.context_engine.state import CollectionState
from lambdas.LlamaPReviewPipeline.context_engine.tool_contract import (
    normalize_tool_step_envelope,
    shared_tool_contract_prompt,
    validate_tool_invocation,
    validate_verification_plan,
)
from lambdas.LlamaPReviewPipeline.context_engine.tools import ToolExecutor
from lambdas.LlamaPReviewPipeline.review.analyzer import PR_ANALYZER_SYSTEM_PROMPT


class _Runtime:
    def __init__(self):
        self.read_calls = []

    def read_text_file_bounded(self, repo, path, *, sha=None, opt_in=None):
        self.read_calls.append((repo, path, sha, opt_in))
        return {
            "outcome": "success",
            "content": "should not be downloaded\n",
            "source_size_bytes": 100_000,
            "bytes_read": 100_000,
            "max_bytes": 2 * 1024 * 1024,
        }


class _TypedOutcomeRuntime(_Runtime):
    def __init__(self, outcome):
        super().__init__()
        self.outcome = outcome

    def read_text_file_bounded(self, repo, path, *, sha=None, opt_in=None):
        self.read_calls.append((repo, path, sha, opt_in))
        return {
            "outcome": self.outcome,
            "content": None,
            "source_size_bytes": 100_000,
            "bytes_read": 0,
            "max_bytes": 2 * 1024 * 1024,
            "error_type": self.outcome,
        }


class _LiteralRuntime(_Runtime):
    def __init__(self, content):
        super().__init__()
        self.content = content

    def read_text_file_bounded(self, repo, path, *, sha=None, opt_in=None):
        self.read_calls.append((repo, path, sha, opt_in))
        size = len(self.content.encode("utf-8"))
        return {
            "outcome": "success",
            "content": self.content,
            "source_size_bytes": size,
            "bytes_read": size,
            "max_bytes": 2 * 1024 * 1024,
        }


def _inventory(*, path="src/large.py", size=100_000, status="complete"):
    return RepoInventory(
        repository="owner/repo",
        requested_sha="abcdef123456",
        status=status,
        items=[{"path": path, "type": "blob", "size": size}],
        discoverable_files={path},
    )


def _state(runtime, inventory):
    return CollectionState(
        pr_details="details",
        pr_content={"file_changes": []},
        repo_full_name="owner/repo",
        head_sha="abcdef123456",
        default_branch="main",
        runtime=runtime,
        accessible_files=set(inventory.discoverable_files),
        repo_inventory=inventory,
    )


def _record_read_event(
    state,
    *,
    path,
    mode="content",
    outcome,
    observed_state,
):
    args = {"path": path, "mode": mode, "reason": "Verify retrieval health."}
    question_id = state.evidence_ledger.register_question(
        question=f"Inspect {path} in {mode} mode.",
        tool="read_file",
        args=args,
    )
    state.evidence_ledger.record_event(
        question_id=question_id,
        tool="read_file",
        args=args,
        outcome=outcome,
        paths=[path] if outcome == "hit" else [],
        source_ref=f"pr_head:{state.head_sha}",
        coverage_type=(
            "exact_path_state"
            if mode == "exact_path_existence"
            else "full_file"
            if outcome == "hit"
            else ""
        ),
        exact_path_state=(
            observed_state if mode == "exact_path_existence" else ""
        ),
        observed_state=observed_state,
    )


class PfrRetrievalContractTest(unittest.TestCase):
    def test_inventory_aware_plan_and_reconcile_share_one_tool_contract(self):
        contract = shared_tool_contract_prompt()
        self.assertNotIn(contract, PR_ANALYZER_SYSTEM_PROMPT)
        self.assertIn(contract, PLAN_PROMPT)
        self.assertIn(contract, PLAN_CONTINUATION_PROMPT)
        self.assertIn(contract, RECONCILE_SYSTEM_PROMPT)
        self.assertNotIn('"provenance_kind": "ci_snapshot"', RECONCILE_PROMPT)
        self.assertIn(
            "Evidence identity, same-question binding",
            RECONCILE_PROMPT,
        )
        self.assertIn(
            "are code-enforced",
            RECONCILE_PROMPT,
        )

    def test_flat_tool_envelope_normalization_is_unique_and_value_preserving(self):
        cases = (
            (
                "search_code",
                {
                    "query": "WidgetFactory(",
                    "reason": "Find callers.",
                    "intent": "external_usage",
                },
            ),
            (
                "read_file",
                {
                    "path": "src/service.py",
                    "reason": "Inspect behavior.",
                    "symbols": ["WidgetFactory", "build"],
                    "mode": "content",
                },
            ),
            (
                "list_dir",
                {
                    "target_path": "src",
                    "max_depth": 2,
                    "reason": "Inspect the bounded module.",
                },
            ),
        )
        for tool, expected_args in cases:
            with self.subTest(tool=tool):
                step = {
                    "question": "Verify the named contract.",
                    "why_it_matters": "It can change merge readiness.",
                    "tool": tool,
                    **expected_args,
                }
                normalized = normalize_tool_step_envelope(step)
                self.assertTrue(normalized.valid)
                self.assertEqual(
                    normalized.action,
                    f"{tool}.args_from_top_level",
                )
                self.assertEqual(normalized.step["args"], expected_args)
                accepted, diagnostics = validate_verification_plan(
                    [step],
                    max_items=1,
                )
                self.assertEqual(diagnostics, [])
                self.assertEqual(len(accepted), 1)
                self.assertEqual(
                    accepted[0]["args"],
                    validate_tool_invocation(tool, expected_args).args,
                )

    def test_tool_envelope_normalization_rejects_ambiguous_or_incomplete_shapes(self):
        base = {
            "question": "Verify the named contract.",
            "why_it_matters": "It can change merge readiness.",
        }
        cases = (
            (
                {
                    **base,
                    "tool": "search_code",
                    "args": {"query": "WidgetFactory", "reason": "Find callers."},
                    "query": "DifferentWidget",
                },
                "conflicting_tool_arg_envelopes",
            ),
            (
                {
                    **base,
                    "tool": "read_file",
                    "query": "WidgetFactory",
                },
                "mixed_or_unknown_top_level_tool_args",
            ),
            (
                {
                    **base,
                    "tool": "list_dir",
                    "target_path": "src",
                    "mystery": "value",
                },
                "mixed_or_unknown_top_level_tool_args",
            ),
            (
                {
                    **base,
                    "tool": "read_file",
                    "reason": "No path was supplied.",
                },
                "path_missing",
            ),
        )
        for step, expected_reason in cases:
            with self.subTest(reason=expected_reason):
                normalized = normalize_tool_step_envelope(step)
                accepted, diagnostics = validate_verification_plan(
                    [step],
                    max_items=1,
                )
                self.assertEqual(accepted, [])
                self.assertTrue(
                    expected_reason in normalized.reasons
                    or any(expected_reason in diagnostic for diagnostic in diagnostics)
                )

        nested = {
            **base,
            "tool": "read_file",
            "args": {
                "path": "src/service.py",
                "symbols": ["WidgetFactory"],
            },
        }
        normalized = normalize_tool_step_envelope(nested)
        self.assertTrue(normalized.valid)
        self.assertEqual(normalized.action, "")
        self.assertEqual(normalized.step, nested)

    def test_literal_search_contract_rejects_regex_and_qualifiers(self):
        self.assertTrue(
            validate_tool_invocation(
                "search_code", {"query": "WidgetFactory(", "reason": "Find callers."}
            ).valid
        )
        self.assertFalse(
            validate_tool_invocation(
                "search_code", {"query": "Widget.*", "reason": "Find callers."}
            ).valid
        )

    def test_list_dir_parent_traversal_cannot_collapse_to_repository_root(self):
        explicit_root = validate_tool_invocation(
            "list_dir",
            {"target_path": "", "reason": "Inspect the bounded root."},
        )
        traversal = validate_tool_invocation(
            "list_dir",
            {"target_path": "../src", "reason": "Invalid scope."},
        )

        self.assertTrue(explicit_root.valid)
        self.assertEqual(explicit_root.args["target_path"], "")
        self.assertFalse(traversal.valid)
        self.assertIn("list_dir_target_path_invalid", traversal.reasons)

    def test_executor_rejects_parent_traversal_before_inventory_render(self):
        runtime = _Runtime()
        state = _state(runtime, _inventory())
        executor = ToolExecutor(state)

        result = executor.execute(
            {
                "function": {
                    "name": "list_dir",
                    "arguments": json.dumps(
                        {
                            "target_path": "../src",
                            "max_depth": 2,
                            "reason": "Inspect the requested directory.",
                        }
                    ),
                }
            },
            question_id="",
        )

        self.assertIn("rejected malformed retrieval request", result)
        self.assertEqual(state.list_calls, 0)
        self.assertEqual(state.tool_events[-1]["outcome"], "error")
        self.assertIn(
            "list_dir_target_path_invalid",
            state.tool_events[-1]["error_kind"],
        )
        self.assertFalse(
            validate_tool_invocation(
                "search_code", {"query": "Widget path:src", "reason": "Find callers."}
            ).valid
        )
        self.assertFalse(
            validate_tool_invocation(
                "search_code", {"query": "Widget OR Gadget", "reason": "Find callers."}
            ).valid
        )
        self.assertFalse(
            validate_tool_invocation(
                "search_code", {"query": "config", "reason": "Find callers."}
            ).valid
        )
        self.assertFalse(
            validate_tool_invocation(
                "search_code",
                {"query": "config value manager", "reason": "Find callers."},
            ).valid
        )
        self.assertTrue(
            validate_tool_invocation(
                "search_code", {"query": "$scope", "reason": "Find callers."}
            ).valid
        )
        self.assertFalse(
            validate_tool_invocation(
                "search_code",
                {"query": "Widget", "reason": "Find callers.", "path": "src"},
            ).valid
        )
        oversized_symbols = validate_tool_invocation(
            "read_file",
            {
                "path": "src/large.py",
                "reason": "Inspect the relevant implementation.",
                "symbols": ["one", "two", "three", "four", "five", "six"],
            },
        )
        self.assertFalse(oversized_symbols.valid)
        self.assertIn("read_file_symbols_exceed_cap", oversized_symbols.reasons)

    def test_read_file_symbol_cap_is_identical_in_shared_model_prompts(self):
        prompt_contract = shared_tool_contract_prompt()
        expected_cap_copy = (
            "symbols array accepts at most 5 literal anchors for every file"
        )
        self.assertIn(expected_cap_copy, prompt_contract)
        for prompt in (
            PLAN_CONTINUATION_PROMPT,
            PLAN_PROMPT,
            RECONCILE_SYSTEM_PROMPT,
        ):
            self.assertIn(expected_cap_copy, prompt)

    def test_literal_enrichment_cannot_invent_a_repository_concept(self):
        runtime = _Runtime()
        state = _state(runtime, _inventory())
        steps, debug = _postprocess_planned_search_steps(
            [
                {
                    "question": "Check compatibility behavior.",
                    "why_it_matters": "Caller behavior could change.",
                    "tool": "search_code",
                    "args": {
                        "query": "InventedRegistry",
                        "reason": "Find compatibility callers.",
                    },
                }
            ],
            state=state,
            entities={},
            named_hints=[],
        )

        self.assertEqual(steps, [])
        self.assertTrue(
            any(item.startswith("drop_ungrounded_literal:model:") for item in debug)
        )

    def test_inventory_path_state_stays_separate_from_unobserved_read_content(self):
        runtime = _Runtime()
        state = _state(runtime, _inventory())
        question_id = state.evidence_ledger.register_question(
            question="Inspect the missing config content.",
            tool="read_file",
            args={"path": "src/missing.py"},
        )

        ToolExecutor(state).execute(
            {
                "function": {
                    "name": "read_file",
                    "arguments": {"path": "src/missing.py"},
                }
            },
            question_id=question_id,
        )

        self.assertEqual(runtime.read_calls, [])
        event = state.evidence_ledger.events[
            state.tool_events[-1]["evidence_event_id"]
        ]
        self.assertEqual(event["exact_path_state"], "absent")
        self.assertEqual(event["observed_state"], "content_unobserved")
        self.assertEqual(event["coverage_type"], "")

    def test_exact_path_mode_records_literal_present_and_absent_proof(self):
        runtime = _Runtime()
        state = _state(runtime, _inventory())
        executor = ToolExecutor(state)

        present = executor.read_file(
            {
                "path": "src/large.py",
                "mode": "exact_path_existence",
                "reason": "Check this exact path.",
            }
        )
        absent = executor.read_file(
            {
                "path": "src/missing.py",
                "mode": "exact_path_existence",
                "reason": "Check this exact path.",
            }
        )

        self.assertEqual(runtime.read_calls, [])
        self.assertEqual(present.metadata["coverage_type"], "exact_path_state")
        self.assertEqual(present.metadata["observed_state"], "present")
        self.assertEqual(absent.metadata["coverage_type"], "exact_path_state")
        self.assertEqual(absent.metadata["observed_state"], "absent")

        executor.execute(
            {
                "function": {
                    "name": "read_file",
                    "arguments": {
                        "path": "src/large.py",
                        "mode": "exact_path_existence",
                        "reason": "Check this exact path.",
                    },
                }
            }
        )
        self.assertEqual(
            state.tool_events[-1]["args"]["mode"], "exact_path_existence"
        )

    def test_exact_path_mode_never_reports_a_known_directory_as_absent(self):
        runtime = _Runtime()
        inventory = RepoInventory(
            repository="owner/repo",
            requested_sha="abcdef123456",
            status="complete",
            items=[{"path": "deploy/prod", "type": "tree"}],
            discoverable_files=set(),
        )
        state = _state(runtime, inventory)

        self.assertEqual(inventory.exact_path_state("deploy/prod"), "directory")
        result = ToolExecutor(state).read_file(
            {
                "path": "deploy/prod",
                "mode": "exact_path_existence",
                "reason": "Check this exact file path.",
            }
        )

        self.assertEqual(result.outcome, "no_hit")
        self.assertEqual(result.error_kind, "exact_path_not_file")
        self.assertEqual(result.metadata["exact_path_state"], "unknown")
        self.assertEqual(result.metadata["observed_state"], "unknown")
        self.assertEqual(runtime.read_calls, [])

    def test_exact_path_mode_uses_one_bounded_probe_for_partial_inventory(self):
        runtime = _Runtime()
        inventory = _inventory(status="partial")
        inventory.discoverable_files.clear()
        state = _state(runtime, inventory)

        result = ToolExecutor(state).read_file(
            {
                "path": "src/probed.py",
                "mode": "exact_path_existence",
                "reason": "Check this exact path.",
            }
        )

        self.assertEqual(result.metadata["observed_state"], "present")
        self.assertEqual(len(runtime.read_calls), 1)
        self.assertIn("src/probed.py", inventory.direct_probe_paths)

    def test_exact_path_probe_treats_typed_object_outcomes_as_present(self):
        for outcome in ("oversize", "binary_or_non_utf8", "directory"):
            with self.subTest(outcome=outcome):
                runtime = _TypedOutcomeRuntime(outcome)
                inventory = _inventory(status="partial")
                inventory.discoverable_files.clear()
                state = _state(runtime, inventory)

                result = ToolExecutor(state).read_file(
                    {
                        "path": "src/probed.py",
                        "mode": "exact_path_existence",
                        "reason": "Check this exact path.",
                    }
                )

                self.assertEqual(result.outcome, "hit")
                self.assertEqual(result.metadata["observed_state"], "present")
                self.assertEqual(len(runtime.read_calls), 1)

    def test_large_content_read_is_rejected_before_download_but_keeps_path_truth(self):
        runtime = _Runtime()
        state = _state(runtime, _inventory())

        result = ToolExecutor(state).read_file({"path": "src/large.py"})

        self.assertEqual(result.outcome, "no_hit")
        self.assertEqual(result.error_kind, "large_read_unaddressable")
        self.assertEqual(result.metadata["exact_path_state"], "present")
        self.assertEqual(result.metadata["observed_state"], "content_unobserved")
        self.assertEqual(runtime.read_calls, [])

    def test_large_read_uses_only_a_literal_already_named_by_the_diff_and_question(self):
        runtime = _Runtime()
        state = _state(runtime, _inventory())
        steps = [
            {
                "question": "Does LargeSymbol preserve its caller contract?",
                "tool": "read_file",
                "args": {"path": "src/large.py", "reason": "Inspect LargeSymbol."},
            }
        ]

        diagnostics = _address_large_read_steps(
            steps,
            state=state,
            entities={"added_symbols": {"LargeSymbol"}},
            named_hints=[],
        )

        self.assertEqual(steps[0]["args"]["symbols"], ["LargeSymbol"])
        self.assertEqual(diagnostics, ["large_read_symbols_attached:src/large.py:1"])

    def test_large_read_without_a_named_literal_stays_unaddressable(self):
        runtime = _Runtime()
        state = _state(runtime, _inventory())
        steps = [
            {
                "question": "Check the behavior.",
                "tool": "read_file",
                "args": {"path": "src/large.py", "reason": "Inspect behavior."},
            }
        ]

        diagnostics = _address_large_read_steps(
            steps,
            state=state,
            entities={},
            named_hints=[],
        )

        self.assertNotIn("symbols", steps[0]["args"])
        self.assertEqual(diagnostics, ["large_read_unaddressable:src/large.py"])

    def test_large_read_filters_model_symbols_to_already_named_literals(self):
        runtime = _Runtime()
        state = _state(runtime, _inventory())
        steps = [
            {
                "question": "Does LargeSymbol preserve its caller contract?",
                "tool": "read_file",
                "args": {
                    "path": "src/large.py",
                    "reason": "Inspect LargeSymbol.",
                    "symbols": ["InventedSymbol", "LargeSymbol"],
                },
            }
        ]

        diagnostics = _address_large_read_steps(
            steps,
            state=state,
            entities={},
            named_hints=[],
        )

        self.assertEqual(steps[0]["args"]["symbols"], ["LargeSymbol"])
        self.assertEqual(
            diagnostics,
            ["large_read_symbols_filtered:src/large.py:1"],
        )

    def test_large_read_drops_fully_ungrounded_model_symbols(self):
        runtime = _Runtime()
        state = _state(runtime, _inventory())
        steps = [
            {
                "question": "Check the behavior.",
                "tool": "read_file",
                "args": {
                    "path": "src/large.py",
                    "reason": "Inspect behavior.",
                    "symbols": ["InventedSymbol"],
                },
            }
        ]

        diagnostics = _address_large_read_steps(
            steps,
            state=state,
            entities={},
            named_hints=[],
        )

        self.assertNotIn("symbols", steps[0]["args"])
        self.assertEqual(diagnostics, ["large_read_unaddressable:src/large.py"])

    def test_target_patch_scoped_literal_selects_only_its_same_file_window(self):
        path, literal = "data/registry.json", "packages/@catalog/signal-handler"
        content = (
            '"packages/@other/signal-handler": "unrelated early record",\n'
            + ('"padding": "unrelated record outside the requested window",\n' * 1200)
            + f'"{literal}": "changed exact-head value",\n'
            + ('"padding": "unrelated trailing record",\n' * 1200)
        )
        self.assertGreater(len(content.encode()), 50 * 1024)
        for supplied in (False, True):
            with self.subTest(supplied=supplied):
                runtime = _LiteralRuntime(content)
                state = _state(runtime, _inventory(path=path, size=len(content.encode())))
                state.pr_content["file_changes"] = [
                    {
                        "file_path": path,
                        "diff": f'-"{literal}": "old",\n+"{literal}": "changed exact-head value",\n',
                    }
                ]
                args = {"path": path, "reason": f"Inspect '{literal}'."}
                if supplied:
                    args["symbols"] = [literal]
                steps = [
                    {"tool": "read_file", "question": "Inspect the changed key.", "args": args}
                ]
                _address_large_read_steps(steps, state=state, entities={}, named_hints=[])
                self.assertIn(literal, steps[0]["args"]["symbols"])
                self.assertEqual(steps[0]["args"]["symbols"][0], literal)
                if not supplied:
                    continue
                result = ToolExecutor(state).read_file(steps[0]["args"])
                self.assertEqual(result.outcome, "hit")
                self.assertEqual(result.metadata["coverage_type"], "file_slice")
                self.assertFalse(result.metadata["backend_full_file_fetched"])
                self.assertEqual(runtime.read_calls, [("owner/repo", path, state.head_sha, None)])
                self.assertEqual(len(state.collected_snippets), 1)
                snippet = state.collected_snippets[0]
                self.assertEqual(snippet["path"], path)
                self.assertIn(literal, snippet["code"])
                self.assertIn("changed exact-head value", snippet["code"])
                self.assertNotIn("unrelated early record", snippet["code"])
                self.assertGreater(snippet["start"], 1000)
                self.assertLessEqual(snippet["end"] - snippet["start"] + 1, 7)
                self.assertTrue(snippet["exact_head_admitted"])

    def test_target_cached_text_grounds_scoped_literal_without_a_patch(self):
        path, literal = "data/registry.json", "packages/@catalog/signal-handler"
        state = _state(_Runtime(), _inventory(path=path))
        state.source_text_cache[path] = {"content": f'"{literal}": "observed"'}
        steps = [
            {
                "tool": "read_file",
                "question": f"Inspect '{literal}'.",
                "args": {"path": path, "symbols": [literal]},
            }
        ]
        _address_large_read_steps(steps, state=state, entities={}, named_hints=[])
        self.assertEqual(steps[0]["args"]["symbols"], [literal])

    def test_model_or_different_file_cannot_ground_a_scoped_literal(self):
        path, literal = "data/registry.json", "packages/@catalog/signal-handler"
        for other_evidence in (False, True):
            with self.subTest(other_evidence=other_evidence):
                state = _state(_Runtime(), _inventory(path=path))
                if other_evidence:
                    state.source_text_cache["data/other.json"] = {
                        "content": f'"{literal}": "elsewhere"'
                    }
                    state.pr_content["file_changes"] = [
                        {"file_path": "data/other.json", "diff": f'+["{path}", "{literal}"]\n'}
                    ]
                steps = [
                    {
                        "tool": "read_file",
                        "question": f"Inspect '{literal}'.",
                        "args": {"path": path, "symbols": [literal]},
                    }
                ]
                _address_large_read_steps(steps, state=state, entities={}, named_hints=[literal])
                self.assertNotIn(literal, steps[0]["args"].get("symbols", []))

    def test_slash_literal_requires_exact_quoted_membership_not_substring(self):
        path, literal = "data/registry.json", "packages/@catalog/signal-handler"
        for observed in (literal, f'"prefix-{literal}"', f'"{literal}-suffix"'):
            for source in ("patch", "cache"):
                with self.subTest(source=source, observed=observed):
                    state = _state(_Runtime(), _inventory(path=path))
                    if source == "patch":
                        state.pr_content["file_changes"] = [
                            {"file_path": path, "diff": "+" + observed}
                        ]
                    else:
                        state.source_text_cache[path] = {"content": observed}
                    steps = [
                        {
                            "tool": "read_file",
                            "question": f"Inspect '{literal}'.",
                            "args": {"path": path, "symbols": [literal]},
                        }
                    ]
                    _address_large_read_steps(steps, state=state, entities={}, named_hints=[])
                    self.assertNotIn(literal, steps[0]["args"].get("symbols", []))

    def test_observed_unsafe_or_target_filename_literals_still_cannot_be_selectors(self):
        path = "data/registry.json"
        for literal in (
            "https://example.invalid/key",
            "https:example.invalid/key",
            "mailto:user@example.invalid/path",
            "/absolute/key",
            "../parent/key",
            "scope/../key",
            "scope/./key",
            "scope\\key",
            path,
            "@catalog/" + "x" * 121,
        ):
            with self.subTest(literal=literal):
                state = _state(_Runtime(), _inventory(path=path))
                state.source_text_cache[path] = {"content": literal}
                state.pr_content["file_changes"] = [
                    {"file_path": path, "diff": f'+"{literal}": "observed"\n'}
                ]
                steps = [
                    {
                        "tool": "read_file",
                        "question": f"Inspect '{literal}'.",
                        "args": {"path": path, "symbols": [literal]},
                    }
                ]
                _address_large_read_steps(steps, state=state, entities={}, named_hints=[])
                self.assertNotIn(literal, steps[0]["args"].get("symbols", []))

    def test_grounded_scoped_literals_keep_the_existing_five_selector_cap(self):
        path = "data/registry.json"
        literals = [f"@catalog/key{number}" for number in range(7)]
        state = _state(_Runtime(), _inventory(path=path))
        state.pr_content["file_changes"] = [
            {
                "file_path": path,
                "diff": "\n".join(f'+"{literal}": "observed"' for literal in literals),
            }
        ]
        steps = [
            {
                "tool": "read_file",
                "question": "Inspect the changed keys.",
                "args": {"path": path, "symbols": literals},
            }
        ]
        _address_large_read_steps(steps, state=state, entities={}, named_hints=[])
        self.assertEqual(steps[0]["args"]["symbols"], literals[:5])

    def test_automatic_scoped_selectors_prefer_keys_not_opaque_values(self):
        path, key = "data/registry.json", "packages/@catalog/signal-handler"
        for value in ("sha512-AbCdEfGhIjKlMnOpQrStUv/WxYz0123456789==", "AbCdEfGhIjKlMnOpQrStUv/WxYz0123456789"):
            with self.subTest(value=value):
                state = _state(_Runtime(), _inventory(path=path))
                state.pr_content["file_changes"] = [
                    {"file_path": path, "diff": f'+"integrity": "{value}",\n+"{key}": "changed"\n'}
                ]
                steps = [{"tool": "read_file", "question": "Inspect the changed entry.", "args": {"path": path}}]
                _address_large_read_steps(steps, state=state, entities={}, named_hints=[])
                self.assertEqual(steps[0]["args"]["symbols"][0], key)
                self.assertNotIn(value, steps[0]["args"]["symbols"])
                # Explicit observed values remain valid literal selectors; only
                # automatic key priority is constrained, not file access.
                steps[0]["args"]["symbols"] = [value]
                _address_large_read_steps(steps, state=state, entities={}, named_hints=[])
                self.assertEqual(steps[0]["args"]["symbols"], [value])

    def test_fetch_health_counts_unaddressable_content_as_failed_retrieval(self):
        runtime = _Runtime()
        state = _state(runtime, _inventory())
        state.planned_read_paths = ["src/large.py"]
        state.read_outcomes["src/large.py"] = "large_read_unaddressable"
        _record_read_event(
            state,
            path="src/large.py",
            outcome="large_read_unaddressable",
            observed_state="content_unobserved",
        )

        health = _fetch_health(
            state,
            {
                "pr_type": "code",
                "verification_plan": [
                    {"tool": "read_file", "args": {"path": "src/large.py"}}
                ],
            },
        )

        self.assertEqual(health["planned_retrieval_status"], "failed")
        self.assertEqual(health["status"], "partial_or_failed_context")
        self.assertEqual(health["planned_content_unobserved_paths"], ["src/large.py"])
        self.assertIn("large_read_unaddressable", health["reasons"])

    def test_fetch_health_marks_mixed_content_and_exact_path_outcomes_degraded(self):
        runtime = _Runtime()
        state = _state(runtime, _inventory(path="src/observed.py", size=10))
        state.planned_read_paths = ["src/observed.py", "src/missing.py"]
        state.planned_read_modes = {
            "src/observed.py": "content",
            "src/missing.py": "exact_path_existence",
        }
        state.read_success_paths.add("src/observed.py")
        _record_read_event(
            state,
            path="src/observed.py",
            outcome="hit",
            observed_state="content_observed",
        )
        _record_read_event(
            state,
            path="src/missing.py",
            mode="exact_path_existence",
            outcome="error",
            observed_state="unknown",
        )
        state.tool_events.append(
            {
                "tool": "read_file",
                "args": {
                    "path": "src/missing.py",
                    "mode": "exact_path_existence",
                },
                "metadata": {"observed_state": "unknown"},
            }
        )

        health = _fetch_health(
            state,
            {
                "pr_type": "code",
                "verification_plan": [
                    {"tool": "read_file", "args": {"path": "src/observed.py"}},
                    {
                        "tool": "read_file",
                        "args": {
                            "path": "src/missing.py",
                            "mode": "exact_path_existence",
                        },
                    },
                ],
            },
        )

        self.assertEqual(health["planned_retrieval_status"], "degraded")
        self.assertEqual(health["status"], "partial_or_failed_context")
        self.assertEqual(
            health["degradation_reason_counts"],
            {"read_failure": 1},
        )
        self.assertEqual(health["reasons"], ["read_failure"])

    def test_fetch_health_keeps_content_and_exact_path_requests_separate(self):
        for events in (
            [
                {
                    "tool": "read_file",
                    "outcome": "error",
                    "args": {"path": "src/service.py", "mode": "content"},
                    "metadata": {"observed_state": "content_unobserved"},
                },
                {
                    "tool": "read_file",
                    "outcome": "hit",
                    "args": {
                        "path": "src/service.py",
                        "mode": "exact_path_existence",
                    },
                    "metadata": {"observed_state": "present"},
                },
            ],
            [
                {
                    "tool": "read_file",
                    "outcome": "hit",
                    "args": {
                        "path": "src/service.py",
                        "mode": "exact_path_existence",
                    },
                    "metadata": {"observed_state": "present"},
                },
                {
                    "tool": "read_file",
                    "outcome": "error",
                    "args": {"path": "src/service.py", "mode": "content"},
                    "metadata": {"observed_state": "content_unobserved"},
                },
            ],
        ):
            with self.subTest(event_order=[item["args"]["mode"] for item in events]):
                runtime = _Runtime()
                state = _state(
                    runtime,
                    _inventory(path="src/service.py", size=100),
                )
                state.planned_read_paths = ["src/service.py"]
                state.planned_read_modes = {
                    "src/service.py": "exact_path_existence"
                }
                state.read_error_paths.add("src/service.py")
                state.read_outcomes["src/service.py"] = "error"
                state.tool_events.extend(events)
                for event in events:
                    _record_read_event(
                        state,
                        path=event["args"]["path"],
                        mode=event["args"]["mode"],
                        outcome=event["outcome"],
                        observed_state=event["metadata"]["observed_state"],
                    )

                health = _fetch_health(
                    state,
                    {
                        "pr_type": "code",
                        "verification_plan": [
                            {
                                "tool": "read_file",
                                "args": {
                                    "path": "src/service.py",
                                    "mode": "content",
                                },
                            },
                            {
                                "tool": "read_file",
                                "args": {
                                    "path": "src/service.py",
                                    "mode": "exact_path_existence",
                                },
                            },
                        ],
                    },
                )

                self.assertEqual(health["planned_read_file_count"], 2)
                self.assertEqual(
                    health["planned_read_requests"],
                    [
                        {"path": "src/service.py", "mode": "content"},
                        {
                            "path": "src/service.py",
                            "mode": "exact_path_existence",
                        },
                    ],
                )
                self.assertEqual(health["planned_retrieval_status"], "degraded")
                self.assertEqual(health["status"], "partial_or_failed_context")


if __name__ == "__main__":
    unittest.main()
