import json
import unittest
from unittest.mock import patch

from tests.unit.fakes import ensure_repo_root_on_path, install_fake_requests_module, set_default_env

ensure_repo_root_on_path()
set_default_env()
install_fake_requests_module()

from lambdas.LlamaPReviewPipeline.context_engine.initialization import initialize_collection
from lambdas.LlamaPReviewPipeline.context_engine.assembler import (
    assemble_context,
    assemble_reconcile_context,
    context_meta,
)
from lambdas.LlamaPReviewPipeline.context_engine.code_extractor import extract_diff_entities
from lambdas.LlamaPReviewPipeline.context_engine.evidence import (
    EvidenceLedger,
    event_supports_answer,
)
from lambdas.LlamaPReviewPipeline.context_engine.packing import (
    ContextSection,
    pack_sections,
    truncate_preserving_current_ci,
)
from lambdas.LlamaPReviewPipeline.context_engine.pfr import (
    PFR_RECONCILE_NEUTRAL_SUMMARY,
    PFRReconcileFailure,
    PFRStructuredOutputError,
    _apply_reconcile_to_ledger,
    _normalize_reconcile_contract,
    _reconcile,
    _strip_reconcile_extra_fields,
    _validate_reconcile_repair_delta,
    collect_context_pfr,
)
from lambdas.LlamaPReviewPipeline.structured_repair import (
    ContractRepairIssue,
    RepairIssueSelection,
)
from lambdas.LlamaPReviewPipeline.context_engine.repo_structure import RepoInventory, fetch_repo_inventory
from lambdas.LlamaPReviewPipeline.context_engine.search_rag import (
    postprocess_search_args,
)
from lambdas.LlamaPReviewPipeline.context_engine.state import CollectionState
from lambdas.LlamaPReviewPipeline.context_engine.tools import ToolExecutor
from lambdas.LlamaPReviewPipeline.deadline import Deadline
from lambdas.LlamaPReviewPipeline.deepseek_client import DeepSeekHTTPError
from lambdas.LlamaPReviewPipeline.review.analyzer import analyze_pr_complexity, build_route_plan_digest


class _Response:
    status_code = 200

    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


class _Runtime:
    def __init__(self, files=None, search_results=None):
        self.files = dict(files or {})
        self.search_results = list(search_results or [])
        self.reads = []
        self.searches = []

    def get_file_content(self, repo, path, *, sha=None):
        self.reads.append((repo, path, sha))
        return self.files.get(path)

    def search_code(self, query, repo):
        self.searches.append((query, repo))
        return list(self.search_results)


class _Client:
    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.calls = []

    def chat(self, messages, **kwargs):
        self.calls.append({"messages": messages, **kwargs})
        payload = self.payloads.pop(0)
        return {
            "choices": [
                {
                    "message": {"role": "assistant", "content": json.dumps(payload)},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"total_tokens": 7},
        }


class _RawClient:
    def __init__(self, contents):
        self.contents = list(contents)
        self.calls = []

    def chat(self, messages, **kwargs):
        self.calls.append({"messages": messages, **kwargs})
        content = self.contents.pop(0)
        if not isinstance(content, str):
            content = json.dumps(content)
        return {
            "choices": [
                {
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"total_tokens": 7},
        }


def _inventory(*paths, status="complete"):
    items = [{"path": path, "type": "blob", "size": 10} for path in paths]
    return RepoInventory(
        repository="owner/repo",
        requested_sha="head1234",
        status=status,
        tree_truncated=status == "partial",
        items=items,
        discoverable_files=set(paths),
    )


def _tree_result(inventory):
    return inventory.render_tree(max_depth=99, include_file_list=True, include_summary=False)


def _plan_payload(*steps):
    return {
        "author_acceptance_criteria": [],
        "verification_plan": list(steps),
    }


class ContextEngineReadinessContractsTest(unittest.TestCase):
    def test_exact_head_answer_eligibility_is_monotonic(self):
        head = "abc123"
        exact = {
            "outcome": "hit",
            "source_ref": f"pr_head:{head}",
        }
        self.assertTrue(
            event_supports_answer(exact, expected_head_sha=head)
        )

        for weaker in (
            {**exact, "outcome": "no_hit"},
            {**exact, "source_ref": "pr_head:def456"},
            {"outcome": "hit", "source_ref": ""},
            {
                "outcome": "hit",
                "source_ref": "default_branch_search",
                "head_reread_outcome": "default_branch_only",
                "search_hit_lineage": [],
            },
            {
                "outcome": "hit",
                "source_ref": "default_branch_search",
                "head_reread_outcome": "partial_head_relocation",
                "search_hit_lineage": [
                    {"outcome": "relocated_at_head", "head_sha": head},
                    {"outcome": "default_branch_only", "head_sha": ""},
                ],
            },
        ):
            with self.subTest(event=weaker):
                self.assertFalse(
                    event_supports_answer(
                        weaker,
                        expected_head_sha=head,
                    )
                )

        relocated = {
            "outcome": "hit",
            "source_ref": "default_branch_search",
            "head_reread_outcome": "relocated_at_head",
            "search_hit_lineage": [
                {"outcome": "relocated_at_head", "head_sha": head},
                {"outcome": "relocated_at_head", "head_sha": head},
            ],
        }
        self.assertTrue(
            event_supports_answer(relocated, expected_head_sha=head)
        )

    def test_event_index_projects_exact_head_answer_eligibility_without_payload(self):
        ledger = EvidenceLedger(expected_head_sha="abc123")
        question_id = ledger.register_question(
            question="Find exact callers.",
            tool="search_code",
            args={"query": "WidgetFactory"},
        )
        exact_event = ledger.record_event(
            question_id=question_id,
            tool="search_code",
            args={"query": "WidgetFactory"},
            outcome="hit",
            paths=["src/caller.py"],
            source_ref="pr_head:abc123",
            coverage_type="search_snippet",
        )
        weaker_events = [
            ledger.record_event(
                question_id=question_id,
                tool="search_code",
                args={"query": f"WidgetFactory-{index}"},
                **shape,
            )
            for index, shape in enumerate(
                (
                    {
                        "outcome": "no_hit",
                        "source_ref": "pr_head:abc123",
                        "coverage_type": "search_snippet",
                    },
                    {
                        "outcome": "error",
                        "source_ref": "pr_head:abc123",
                        "coverage_type": "search_snippet",
                    },
                    {
                        "outcome": "hit",
                        "source_ref": "pr_head:def456",
                        "coverage_type": "search_snippet",
                    },
                    {
                        "outcome": "hit",
                        "source_ref": "",
                        "coverage_type": "search_snippet",
                    },
                    {
                        "outcome": "hit",
                        "source_ref": "default_branch_search",
                        "head_reread_outcome": "default_branch_only",
                        "coverage_type": "search_snippet",
                    },
                    {
                        "outcome": "hit",
                        "source_ref": "default_branch_search",
                        "head_reread_outcome": "partial_head_relocation",
                        "coverage_type": "search_snippet",
                        "search_hit_lineage": [
                            {
                                "outcome": "relocated_at_head",
                                "head_sha": "abc123",
                            },
                            {
                                "outcome": "default_branch_only",
                                "head_sha": "",
                            },
                        ],
                    },
                )
            )
        ]

        event_index = {
            event["event_id"]: event
            for event in ledger.event_index()["events"]
        }
        self.assertTrue(event_index[exact_event]["answer_eligible"])
        for event_id in weaker_events:
            self.assertFalse(event_index[event_id]["answer_eligible"])
        serialized = ledger.compact_event_index_text()
        self.assertNotIn("src/caller.py", serialized)
        self.assertNotIn("WidgetFactory", serialized)

    def test_event_index_projects_exact_question_and_evidence_identities(self):
        ledger = EvidenceLedger(expected_head_sha="abc123")
        first_question = ledger.register_question(
            question="Inspect the first producer.",
            tool="read_file",
            args={"path": "src/first.py"},
        )
        first_event = ledger.record_event(
            question_id=first_question,
            tool="read_file",
            args={"path": "src/first.py"},
            outcome="hit",
            paths=["src/first.py"],
            source_ref="pr_head:abc123",
            coverage_type="full_file",
        )
        no_hit_event = ledger.record_event(
            question_id=first_question,
            tool="read_file",
            args={"path": "src/first.py", "symbols": ["missing"]},
            outcome="no_hit",
            paths=[],
            source_ref="pr_head:abc123",
            coverage_type="file_slice",
        )
        second_question = ledger.register_question(
            question="Inspect the second producer.",
            tool="read_file",
            args={"path": "src/second.py"},
        )
        second_event = ledger.record_event(
            question_id=second_question,
            tool="read_file",
            args={"path": "src/second.py"},
            outcome="hit",
            paths=["src/second.py"],
            source_ref="pr_head:abc123",
            coverage_type="full_file",
        )

        index = ledger.event_index()
        self.assertEqual(
            [item["question_id"] for item in index["questions"]],
            [first_question, second_question],
        )
        self.assertEqual(
            [item["event_id"] for item in index["events"]],
            [first_event, no_hit_event, second_event],
        )
        event_by_id = {
            item["event_id"]: item for item in index["events"]
        }
        self.assertTrue(event_by_id[first_event]["answer_eligible"])
        self.assertFalse(event_by_id[no_hit_event]["answer_eligible"])
        self.assertTrue(event_by_id[second_event]["answer_eligible"])
        self.assertNotIn("obligation_evidence_capabilities", index)
        serialized = ledger.compact_event_index_text()
        self.assertNotIn("src/first.py", serialized)
        self.assertNotIn("src/second.py", serialized)

    def test_default_branch_only_hit_cannot_answer_ledger_question(self):
        ledger = EvidenceLedger(expected_head_sha="abc123")
        question_id = ledger.register_question(
            question="Where is the caller?",
            tool="search_code",
            args={"query": "caller("},
        )
        event_id = ledger.record_event(
            question_id=question_id,
            tool="search_code",
            args={"query": "caller("},
            outcome="hit",
            paths=["src/caller.py"],
            source_ref="default_branch_search",
            head_reread_outcome="default_branch_only",
            search_hit_lineage=[
                {
                    "path": "src/caller.py",
                    "outcome": "literal_missing_at_head",
                    "head_sha": "abc123",
                }
            ],
        )

        resolution = ledger.resolve(
            question_id=question_id,
            status="answered",
            evidence_refs=[event_id],
            conclusion="A default-branch caller was found.",
        )

        self.assertEqual(resolution["status"], "unknown")
        self.assertEqual(resolution["evidence_refs"], [])

    def test_context_metadata_carries_the_exact_collection_head(self):
        state = CollectionState(
            pr_details="details",
            pr_content={},
            repo_full_name="owner/repo",
            head_sha="abc123",
            default_branch="main",
            runtime=_Runtime(),
        )

        self.assertEqual(context_meta(state)["head_sha"], "abc123")

    def test_inventory_is_single_request_dotfile_complete_and_secret_safe(self):
        payload = {
            "truncated": True,
            "tree": [
                {"path": "AGENTS.md", "type": "blob", "size": 10},
                {"path": ".github/copilot-instructions.md", "type": "blob", "size": 10},
                {"path": ".gitignore", "type": "blob", "size": 10},
                {"path": ".env", "type": "blob", "size": 10},
                {"path": ".env.example", "type": "blob", "size": 10},
                {"path": "keys/private.pem", "type": "blob", "size": 10},
                {"path": "src/app.py", "type": "blob", "size": 10},
            ],
        }
        with patch("requests.get", return_value=_Response(payload)) as request:
            inventory = fetch_repo_inventory("owner/repo", token="token", sha="head1234")

        self.assertEqual(request.call_count, 1)
        self.assertEqual(inventory.status, "partial")
        self.assertIn(".gitignore", inventory.discoverable_files)
        self.assertIn(".env.example", inventory.discoverable_files)
        self.assertEqual(inventory.owner_doc_paths, ["AGENTS.md", ".github/copilot-instructions.md"])
        self.assertEqual(inventory.excluded_sensitive, {".env", "keys/private.pem"})

    def test_initialize_and_list_dir_reuse_one_inventory(self):
        inventory = _inventory("AGENTS.md", ".github/copilot-instructions.md", "src/app.py")
        with patch(
            "lambdas.LlamaPReviewPipeline.context_engine.initialization.get_repo_structure_for_llm",
            return_value=_tree_result(inventory),
        ) as initial_tree, patch(
            "lambdas.LlamaPReviewPipeline.context_engine.tools.get_repo_structure_for_llm",
            side_effect=AssertionError("list_dir must not refetch the Git tree"),
        ):
            state = initialize_collection(
                runtime=_Runtime(),
                github_token="token",
                repo_full_name="owner/repo",
                pr_content={"file_changes": []},
                pr_details="details",
                head_sha="head1234",
                default_branch="main",
            )
            result = ToolExecutor(state).execute(
                {"id": "list", "function": {"name": "list_dir", "arguments": json.dumps({"target_path": "src"})}}
            )

        self.assertEqual(initial_tree.call_count, 1)
        self.assertIn("app.py", result)

    def test_partial_inventory_direct_probe_and_removed_path_have_typed_outcomes(self):
        runtime = _Runtime({"src/hidden.py": "def hidden():\n    return True\n"})
        inventory = _inventory("src/visible.py", status="partial")
        state = CollectionState(
            pr_details="details",
            pr_content={"file_changes": []},
            repo_full_name="owner/repo",
            head_sha="head1234",
            default_branch="main",
            runtime=runtime,
            repo_inventory=inventory,
            accessible_files=set(inventory.discoverable_files),
            removed_paths={"src/removed.py"},
        )
        executor = ToolExecutor(state)
        executor.execute(
            {"id": "probe", "function": {"name": "read_file", "arguments": json.dumps({"path": "src/hidden.py"})}}
        )
        executor.execute(
            {"id": "removed", "function": {"name": "read_file", "arguments": json.dumps({"path": "src/removed.py"})}}
        )

        self.assertEqual(state.tool_events[0]["outcome"], "hit")
        self.assertIn("src/hidden.py", inventory.direct_probe_paths)
        self.assertEqual(state.tool_events[1]["outcome"], "removed_path")
        self.assertEqual(state.tool_events[1]["head_reread_outcome"], "removed_at_head")
        self.assertNotIn(("owner/repo", "src/removed.py", "head1234"), runtime.reads)

    def test_search_results_never_expose_sensitive_path_content(self):
        runtime = _Runtime(
            search_results=[
                {"path": ".env", "content": "API_KEY=do-not-render"},
                {"path": "src/app.py", "content": "def run():\n    return True\n"},
            ]
        )
        inventory = _inventory("src/app.py")
        state = CollectionState(
            pr_details="details",
            pr_content={"file_changes": []},
            repo_full_name="owner/repo",
            head_sha="head1234",
            default_branch="main",
            runtime=runtime,
            repo_inventory=inventory,
            accessible_files={"src/app.py"},
            max_search_calls=1,
        )
        result = ToolExecutor(state).execute(
            {
                "id": "search",
                "function": {
                    "name": "search_code",
                    "arguments": json.dumps({"query": "run(", "reason": "Find callers."}),
                },
            }
        )

        self.assertNotIn("do-not-render", result)
        self.assertNotIn(".env", {snippet["path"] for snippet in state.collected_snippets})
        self.assertIn(".env", inventory.excluded_sensitive)

    def test_evidence_ledger_ids_are_stable_and_no_hit_cannot_answer(self):
        first = EvidenceLedger()
        second = EvidenceLedger()
        args = {"query": "Widget(", "reason": "Find callers."}
        q1 = first.register_question(question="Where is Widget used?", tool="search_code", args=args)
        q2 = second.register_question(question="Where is Widget used?", tool="search_code", args=args)
        e1 = first.record_event(question_id=q1, tool="search_code", args=args, outcome="no_hit")
        e2 = second.record_event(question_id=q2, tool="search_code", args=args, outcome="no_hit")
        resolution = first.resolve(question_id=q1, status="answered", evidence_refs=[e1], conclusion="No usages")

        self.assertEqual((q1, e1), (q2, e2))
        self.assertEqual(resolution["status"], "unknown")
        self.assertEqual(resolution["evidence_refs"], [])

    def test_reconcile_uses_event_identity_when_question_text_is_ambiguous(self):
        state = CollectionState(
            pr_details="details",
            pr_content={},
            repo_full_name="owner/repo",
            head_sha="head1234",
            default_branch="main",
            runtime=_Runtime(),
        )
        question = "Where is Worker used?"
        first_id = state.evidence_ledger.register_question(
            question=question,
            tool="search_code",
            args={"query": "Worker"},
        )
        second_id = state.evidence_ledger.register_question(
            question=question,
            tool="search_code",
            args={"query": "Worker("},
        )
        first_event = state.evidence_ledger.record_event(
            question_id=first_id,
            tool="search_code",
            args={"query": "Worker"},
            outcome="hit",
            paths=["a.py"],
            source_ref="pr_head:head1234",
        )
        second_event = state.evidence_ledger.record_event(
            question_id=second_id,
            tool="search_code",
            args={"query": "Worker("},
            outcome="hit",
            paths=["b.py"],
            source_ref="pr_head:head1234",
        )
        self.assertEqual(state.evidence_ledger.question_id_for_text(question), "")

        reconcile = _apply_reconcile_to_ledger(
            {
                "summary": "Worker usage was found.",
                "answered": [
                    {
                        "question": question,
                        "evidence_refs": [second_event],
                        "evidence": "b.py calls Worker.",
                    }
                ],
                "unresolved_gaps": [],
                "followups": [],
                "complete": True,
            },
            state,
        )

        self.assertEqual(len(reconcile["answered"]), 1)
        self.assertEqual(reconcile["answered"][0]["question_id"], second_id)
        self.assertEqual(
            reconcile["answered"][0]["evidence_refs"], [second_event]
        )
        self.assertNotEqual(
            reconcile["answered"][0]["evidence_refs"], [first_event]
        )

    def test_evidence_binding_failure_stays_internal_context_truth(self):
        state = CollectionState(
            pr_details="details",
            pr_content={},
            repo_full_name="owner/repo",
            head_sha="head1234",
            default_branch="main",
            runtime=_Runtime(),
        )
        question_id = state.evidence_ledger.register_question(
            question="Does the changed guard run?",
            tool="read_file",
            args={"path": "src/guard.py"},
        )

        reconcile = _apply_reconcile_to_ledger(
            {
                "summary": "The guard was reviewed.",
                "answered": [
                    {
                        "question_id": question_id,
                        "question": "Does the changed guard run?",
                        "evidence_refs": [],
                        "evidence": "The guard runs.",
                    }
                ],
                "unresolved_gaps": [],
                "followups": [],
                "complete": True,
            },
            state,
        )

        self.assertFalse(reconcile["complete"])
        self.assertEqual(reconcile["answered"], [])
        self.assertEqual(reconcile["unresolved_gaps"], [])
        self.assertEqual(reconcile["_evidence_binding_failure_count"], 1)
        resolution = state.evidence_ledger.resolutions[question_id]
        self.assertEqual(resolution["status"], "unknown")
        self.assertEqual(resolution["conclusion"], "")
        self.assertEqual(resolution["how_to_check"], "")

    def test_manual_reconcile_unknown_gets_code_owned_stable_resolution_id(self):
        ledger = EvidenceLedger()
        first_question_id = ledger.register_unresolved_gap(
            source_slot="owner/repo:head:pfr_reconcile:1:0",
            question="Could not verify whether the smoke test completed.",
            how_to_check="Run the smoke test.",
        )
        second_question_id = ledger.register_unresolved_gap(
            source_slot="owner/repo:head:pfr_reconcile:1:0",
            question="Completion of the required smoke scenarios is not visible.",
            how_to_check="Execute the declared scenarios.",
        )
        first_resolution = ledger.resolve(
            question_id=first_question_id,
            status="unknown",
            conclusion="First wording.",
        )
        second_resolution = ledger.resolve(
            question_id=second_question_id,
            status="unknown",
            conclusion="Improved wording.",
        )

        self.assertEqual(first_question_id, second_question_id)
        self.assertEqual(first_resolution["id"], second_resolution["id"])
        self.assertEqual(
            ledger.questions[first_question_id]["lifecycle"], "derived_gap"
        )

        state = CollectionState(
            pr_details="details",
            pr_content={},
            repo_full_name="owner/repo",
            head_sha="head",
            default_branch="main",
            runtime=_Runtime(),
        )
        reconciled = _apply_reconcile_to_ledger(
            {
                "answered": [],
                "unresolved_gaps": [
                    {
                        "question_id": "manual_validation",
                        "claim": "Could not verify whether the smoke test completed.",
                        "how_to_check": "Run the smoke test.",
                    }
                ],
            },
            state,
            round_index=1,
        )
        unknown = reconciled["unresolved_gaps"][0]
        self.assertTrue(unknown["question_id"].startswith("q_"))
        self.assertTrue(unknown["resolution_id"].startswith("res_"))
        self.assertNotEqual(unknown["question_id"], "manual_validation")
        self.assertEqual(
            state.evidence_ledger.questions[unknown["question_id"]]["lifecycle"],
            "derived_gap",
        )

    def test_reconcile_sanitizer_strips_extra_fields_with_content_free_counts(self):
        sanitized, normalizations = _strip_reconcile_extra_fields(
            {
                "summary": "Evidence was reconciled.",
                "answered": [
                    {
                        "question_id": "q_answered",
                        "question": "What changed?",
                        "evidence_refs": ["ev_answered"],
                        "evidence": "A changed path was read.",
                        "model_note": "must not persist",
                    }
                ],
                "unresolved_gaps": [
                    {
                        "question_id": "q_unknown",
                        "claim": "Could not verify whether the build passes.",
                        "how_to_check": "Run the build.",
                        "affects_merge": True,
                        "hidden_reasoning": "must not persist",
                    }
                ],
                "followups": [
                    {
                        "question": "Read the contract.",
                        "tool": "read_file",
                        "args": {"path": "src/contract.py"},
                        "priority": "model-owned extra",
                    }
                ],
                "complete": False,
                "debug": "must not persist",
            }
        )

        self.assertNotIn("debug", sanitized)
        self.assertNotIn("model_note", sanitized["answered"][0])
        self.assertNotIn(
            "hidden_reasoning", sanitized["unresolved_gaps"][0]
        )
        self.assertNotIn("priority", sanitized["followups"][0])
        self.assertEqual(
            normalizations,
            [
                "extra_root_fields_removed:1",
                "extra_answered_fields_removed:1",
                "extra_unresolved_gap_fields_removed:2",
                "extra_followup_fields_removed:1",
            ],
        )

    def test_reconcile_projection_does_not_rewrite_model_claims(self):
        claims = (
            "The local package parser rejects invalid version syntax.",
            "The Java validator rejects unsupported Java values before persistence.",
            "The in-repo registry returns not available for a missing local key.",
            "The npm adapter build fails when local configuration is empty.",
            "Spring Boot version 4.1.0 may not exist.",
        )
        for claim in claims:
            with self.subTest(claim=claim):
                projected, normalizations = _strip_reconcile_extra_fields(
                    {
                        "summary": claim,
                        "answered": [{"evidence": claim}],
                        "unresolved_gaps": [],
                        "followups": [],
                        "complete": True,
                    }
                )
                self.assertEqual(projected["summary"], claim)
                self.assertEqual(projected["answered"][0]["evidence"], claim)
                self.assertEqual(normalizations, [])

    def test_exact_head_answer_remains_bound_without_claim_classification(self):
        state = CollectionState(
            pr_details="details",
            pr_content={},
            repo_full_name="owner/repo",
            head_sha="head",
            default_branch="main",
            runtime=_Runtime(),
        )
        question_id = state.evidence_ledger.register_question(
            question="Is Spring Boot 9.9.9 released?",
            tool="read_file",
            args={"path": "pom.xml"},
        )
        event_id = state.evidence_ledger.record_event(
            question_id=question_id,
            tool="read_file",
            args={"path": "pom.xml"},
            outcome="hit",
            paths=["pom.xml"],
            source_ref="pr_head:head",
            coverage_type="full_file",
            observed_state="content_observed",
        )
        projected, _ = _strip_reconcile_extra_fields(
            {
                "summary": "The release is unavailable.",
                "answered": [
                    {
                        "question_id": question_id,
                        "question": "Is Spring Boot 9.9.9 released?",
                        "evidence_refs": [event_id],
                        "evidence": "Spring Boot 9.9.9 is unreleased.",
                    }
                ],
                "unresolved_gaps": [],
                "followups": [],
                "complete": True,
            }
        )

        reconciled = _apply_reconcile_to_ledger(projected, state)

        self.assertEqual(len(reconciled["answered"]), 1)
        self.assertEqual(reconciled["unresolved_gaps"], [])
        self.assertEqual(
            state.evidence_ledger.resolutions[question_id]["status"],
            "answered",
        )
        self.assertTrue(reconciled["complete"])

    def test_explicit_question_and_hit_refs_allow_terminal_punctuation_drift(self):
        state = CollectionState(
            pr_details="details",
            pr_content={},
            repo_full_name="owner/repo",
            head_sha="head",
            default_branch="main",
            runtime=_Runtime(),
        )
        question_id = state.evidence_ledger.register_question(
            question="Where is Worker used?",
            tool="search_code",
            args={"query": "Worker"},
        )
        event_id = state.evidence_ledger.record_event(
            question_id=question_id,
            tool="search_code",
            args={"query": "Worker"},
            outcome="hit",
            paths=["src/worker.py"],
            source_ref="pr_head:head",
        )

        reconciled = _apply_reconcile_to_ledger(
            {
                "summary": "One question was answered.",
                "answered": [
                    {
                        "question_id": question_id,
                        "question": "Where is Worker used",
                        "evidence_refs": [event_id],
                        "evidence": "src/worker.py contains a use.",
                    }
                ],
                "unresolved_gaps": [],
                "followups": [],
                "complete": True,
            },
            state,
        )

        self.assertEqual(len(reconciled["answered"]), 1)
        self.assertEqual(reconciled["unresolved_gaps"], [])
        self.assertTrue(reconciled["complete"])

    def test_conflicting_answers_for_one_question_fail_closed_to_unknown(self):
        state = CollectionState(
            pr_details="details",
            pr_content={},
            repo_full_name="owner/repo",
            head_sha="head",
            default_branch="main",
            runtime=_Runtime(),
        )
        question_id = state.evidence_ledger.register_question(
            question="Does the guard run?",
            tool="read_file",
            args={"path": "src/guard.py"},
        )
        event_id = state.evidence_ledger.record_event(
            question_id=question_id,
            tool="read_file",
            args={"path": "src/guard.py"},
            outcome="hit",
            paths=["src/guard.py"],
        )
        base = {
            "question_id": question_id,
            "question": "Does the guard run?",
            "evidence_refs": [event_id],
        }

        reconciled = _apply_reconcile_to_ledger(
            {
                "summary": "The question was answered twice.",
                "answered": [
                    {**base, "evidence": "The guard runs."},
                    {**base, "evidence": "The guard does not run."},
                ],
                "unresolved_gaps": [],
                "followups": [],
                "complete": True,
            },
            state,
        )

        self.assertEqual(reconciled["answered"], [])
        self.assertEqual(reconciled["unresolved_gaps"], [])
        self.assertEqual(
            state.evidence_ledger.resolutions[question_id]["status"],
            "unknown",
        )
        self.assertFalse(reconciled["complete"])
        self.assertEqual(
            reconciled["summary"], PFR_RECONCILE_NEUTRAL_SUMMARY
        )

    def test_reconcile_never_substitutes_a_hit_for_explicit_mismatched_refs(self):
        state = CollectionState(
            pr_details="details",
            pr_content={},
            repo_full_name="owner/repo",
            head_sha="head1234",
            default_branch="main",
            runtime=_Runtime(),
        )
        question = "Where is Worker used?"
        first_id = state.evidence_ledger.register_question(
            question=question,
            tool="search_code",
            args={"query": "Worker"},
        )
        second_id = state.evidence_ledger.register_question(
            question=question,
            tool="search_code",
            args={"query": "Worker("},
        )
        first_event = state.evidence_ledger.record_event(
            question_id=first_id,
            tool="search_code",
            args={"query": "Worker"},
            outcome="hit",
            paths=["a.py"],
        )
        second_event = state.evidence_ledger.record_event(
            question_id=second_id,
            tool="search_code",
            args={"query": "Worker("},
            outcome="hit",
            paths=["b.py"],
        )

        reconcile = _apply_reconcile_to_ledger(
            {
                "summary": "Worker usage was found.",
                "answered": [
                    {
                        "question_id": first_id,
                        "question": question,
                        "evidence_refs": [second_event],
                        "evidence": "b.py calls Worker.",
                    }
                ],
                "unresolved_gaps": [],
                "followups": [],
                "complete": True,
            },
            state,
        )

        self.assertEqual(reconcile["answered"], [])
        self.assertEqual(reconcile["unresolved_gaps"], [])
        self.assertEqual(
            state.evidence_ledger.resolutions[first_id]["evidence_refs"], []
        )
        self.assertNotEqual(
            state.evidence_ledger.resolutions[first_id]["evidence_refs"],
            [first_event],
        )

    def test_reconcile_omitted_refs_bind_only_one_same_question_hit(self):
        state = CollectionState(
            pr_details="details",
            pr_content={},
            repo_full_name="owner/repo",
            head_sha="head1234",
            default_branch="main",
            runtime=_Runtime(),
        )
        question_id = state.evidence_ledger.register_question(
            question="Where is Worker used?",
            tool="search_code",
            args={"query": "Worker"},
        )
        state.evidence_ledger.record_event(
            question_id=question_id,
            tool="search_code",
            args={"query": "Worker", "attempt": 1},
            outcome="hit",
            paths=["a.py"],
        )
        state.evidence_ledger.record_event(
            question_id=question_id,
            tool="search_code",
            args={"query": "Worker", "attempt": 2},
            outcome="hit",
            paths=["b.py"],
        )

        reconcile = _apply_reconcile_to_ledger(
            {
                "summary": "Worker usage was found.",
                "answered": [
                    {
                        "question_id": question_id,
                        "question": "Where is Worker used?",
                        "evidence": "Worker appears in the repository.",
                    }
                ],
                "unresolved_gaps": [],
                "followups": [],
                "complete": True,
            },
            state,
        )

        self.assertEqual(reconcile["answered"], [])
        self.assertEqual(reconcile["unresolved_gaps"], [])
        self.assertEqual(
            state.evidence_ledger.resolutions[question_id]["status"],
            "unknown",
        )

    def test_reconcile_does_not_bind_mismatched_explicit_question_id_hit(self):
        state = CollectionState(
            pr_details="details",
            pr_content={},
            repo_full_name="owner/repo",
            head_sha="head1234",
            default_branch="main",
            runtime=_Runtime(),
        )
        alpha_id = state.evidence_ledger.register_question(
            question="Where is Alpha used?",
            tool="search_code",
            args={"query": "Alpha"},
        )
        alpha_event = state.evidence_ledger.record_event(
            question_id=alpha_id,
            tool="search_code",
            args={"query": "Alpha"},
            outcome="hit",
            paths=["alpha.py"],
        )
        beta_id = state.evidence_ledger.register_question(
            question="Where is Beta used?",
            tool="search_code",
            args={"query": "Beta"},
        )
        state.evidence_ledger.record_event(
            question_id=beta_id,
            tool="search_code",
            args={"query": "Beta"},
            outcome="no_hit",
        )

        reconcile = _apply_reconcile_to_ledger(
            {
                "summary": "One lookup produced evidence.",
                "answered": [
                    {
                        "question_id": alpha_id,
                        "question": "Where is Beta used?",
                        "evidence": "Beta appears in the repository.",
                    }
                ],
                "unresolved_gaps": [],
                "followups": [],
                "complete": True,
            },
            state,
        )

        self.assertEqual(reconcile["answered"], [])
        self.assertEqual(reconcile["unresolved_gaps"], [])
        self.assertEqual(
            state.evidence_ledger.resolutions[beta_id]["evidence_refs"], []
        )
        self.assertNotIn(
            alpha_event,
            state.evidence_ledger.resolutions[beta_id]["evidence_refs"],
        )

    def test_evidence_ledger_explicit_refs_are_all_or_nothing(self):
        ledger = EvidenceLedger()
        first_id = ledger.register_question(
            question="Where is Alpha used?",
            tool="search_code",
            args={"query": "Alpha"},
        )
        first_hit = ledger.record_event(
            question_id=first_id,
            tool="search_code",
            args={"query": "Alpha"},
            outcome="hit",
            paths=["alpha.py"],
        )
        first_no_hit = ledger.record_event(
            question_id=first_id,
            tool="search_code",
            args={"query": "Alpha fallback"},
            outcome="no_hit",
        )
        second_id = ledger.register_question(
            question="Where is Beta used?",
            tool="search_code",
            args={"query": "Beta"},
        )
        second_hit = ledger.record_event(
            question_id=second_id,
            tool="search_code",
            args={"query": "Beta"},
            outcome="hit",
            paths=["beta.py"],
        )

        for refs in (
            [first_hit, second_hit],
            [first_hit, first_no_hit],
            [first_hit, "ev_unknown"],
        ):
            with self.subTest(refs=refs):
                resolution = ledger.resolve(
                    question_id=first_id,
                    status="answered",
                    evidence_refs=refs,
                    conclusion="The lookup answered the question.",
                )
                self.assertEqual(resolution["status"], "unknown")
                self.assertEqual(resolution["evidence_refs"], [])

        duplicate = ledger.resolve(
            question_id=first_id,
            status="answered",
            evidence_refs=[first_hit, first_hit],
            conclusion="The same hit was repeated.",
        )
        self.assertEqual(duplicate["status"], "answered")
        self.assertEqual(duplicate["evidence_refs"], [first_hit])

    def test_diff_entities_ignore_prose_but_keep_extensionless_code(self):
        content = {
            "file_changes": [
                {
                    "file_path": "README.md",
                    "diff": "+The class FakeManager is the conceptual model.\n",
                },
                {"file_path": "script", "diff": "+def real_handler(value):\n+    return value\n"},
            ]
        }
        entities = extract_diff_entities(content)

        self.assertNotIn("FakeManager", entities["added_symbols"])
        self.assertIn("real_handler", entities["added_symbols"])

    def test_parameter_adoption_is_bound_to_changed_signatures(self):
        content = {
            "file_changes": [
                {
                    "file_path": "src/cache.py",
                    "diff": (
                        "-def build_cache(source):\n"
                        "+def build_cache(source, timeout=None):\n"
                        "+    return source\n"
                    ),
                },
                {
                    "file_path": "src/index.js",
                    "diff": (
                        "+const abs = path.join(ROOT, file);\n"
                        "+// Store: one flat file; Tolerated: unreadable inputs.\n"
                        "+await new Promise((resolve) => stream.end(resolve));\n"
                        "+const mapItems = (items, limit = 10) => items.slice(0, limit);\n"
                    ),
                },
                {
                    "file_path": "worker.go",
                    "diff": "+func Fetch(ctx context.Context, retries int) error { return nil }\n",
                },
                {
                    "file_path": "src/other_cache.py",
                    "diff": "+def build_cache(deadline=None):\n+    return deadline\n",
                },
                {
                    "file_path": "Cache.java",
                    "diff": "+public String cacheName() { return \"primary\"; }\n",
                },
            ]
        }

        entities = extract_diff_entities(content)

        self.assertNotIn("abs", entities["added_symbols"])
        self.assertNotIn("Promise", entities["added_symbols"])
        self.assertNotIn("String", entities["added_symbols"])
        self.assertIn("cacheName", entities["added_symbols"])
        self.assertNotIn("abs", entities["added_params"])
        self.assertNotIn(("abs", "abs"), entities["parameter_adoptions"])
        self.assertIn(("build_cache", "timeout"), entities["parameter_adoptions"])
        self.assertNotIn(("mapItems", "limit"), entities["parameter_adoptions"])
        self.assertNotIn(("Fetch", "retries"), entities["parameter_adoptions"])
        self.assertNotIn(("build_cache", "deadline"), entities["parameter_adoptions"])
        self.assertNotIn(("build_cache", "source"), entities["parameter_adoptions"])

    def test_unknown_extension_structural_signatures_seed_retrieval(self):
        content = {
            "file_changes": [
                {
                    "file_path": "src/service.futurelang",
                    "diff": (
                        "-function dispatch(request) {\n"
                        "+function dispatch(request, timeout) {\n"
                    ),
                },
            ]
        }

        entities = extract_diff_entities(content)
        queries, _debug = postprocess_search_args(
            [],
            entities=entities,
            pr_content=content,
            max_total=2,
        )

        self.assertIn("dispatch", entities["removed_symbols"])
        self.assertIn(("dispatch", "timeout"), entities["parameter_adoptions"])
        self.assertEqual(
            [item["query"] for item in queries],
            ["dispatch", "dispatch timeout"],
        )

    def test_removed_public_rust_struct_gets_reserved_usage_search(self):
        content = {
            "file_changes": [
                {
                    "file_path": "src/directory.rs",
                    "diff": (
                        "-pub struct LegacyDirectory {\n"
                        "-    client: Client,\n"
                        "+pub(crate) struct NewDirectory {\n"
                        "+    client: Client,\n"
                        " impl LegacyDirectory {\n"
                    ),
                }
            ]
        }

        entities = extract_diff_entities(content)
        queries, _debug = postprocess_search_args(
            [],
            entities=entities,
            pr_content=content,
            max_total=1,
        )

        self.assertIn("LegacyDirectory", entities["removed_symbols"])
        self.assertIn("NewDirectory", entities["added_symbols"])
        self.assertEqual(
            [item["query"] for item in queries],
            ["LegacyDirectory"],
        )

    def test_section_packing_preserves_required_contract_and_cap(self):
        packed = pack_sections(
            [
                ContextSection("snippets", "## Related Context\n" + "x" * 1000, priority=50),
                ContextSection("ledger", "## Verification Ledger\nq=unknown", priority=0, required=True, min_chars=40),
                ContextSection("health", "## Collection Summary\nhealthy", priority=0, required=True, min_chars=40),
            ],
            240,
        )
        self.assertLessEqual(len(packed), 240)
        self.assertIn("## Verification Ledger", packed)
        self.assertIn("## Collection Summary", packed)

    def test_large_reconcile_snapshot_keeps_ledger_and_collection_summary(self):
        state = CollectionState(
            pr_details="details",
            pr_content={
                "file_changes": [
                    {
                        "file_path": "src/service.py",
                        "change_type": "modified",
                        "additions": 1,
                        "deletions": 0,
                    }
                ]
            },
            repo_full_name="owner/repo",
            head_sha="head1234",
            default_branch="main",
            runtime=_Runtime(),
            max_context_chars=300000,
        )
        state.collected_snippets = [
            {
                "path": "src/huge_generated.py",
                "start": 1,
                "end": 250000,
                "kind": "usage",
                "source": "read_file",
                "code": "x" * 250000,
            }
        ]
        question_id = state.evidence_ledger.register_question(
            question="VERIFY_LEDGER_MARKER",
            tool="read_file",
            args={"path": "src/service.py"},
        )
        evidence_id = state.evidence_ledger.record_event(
            question_id=question_id,
            tool="read_file",
            args={"path": "src/service.py"},
            outcome="hit",
            paths=["src/service.py"],
        )
        state.evidence_ledger.resolve(
            question_id=question_id,
            status="answered",
            evidence_refs=[evidence_id],
        )
        state.finish_summary = "COLLECTION_SUMMARY_MARKER"

        snapshot = assemble_context(state, max_chars=180000)

        self.assertLessEqual(len(snapshot), 180000)
        self.assertIn("## Verification Ledger", snapshot)
        self.assertIn("VERIFY_LEDGER_MARKER", snapshot)
        self.assertIn("## Collection Summary", snapshot)
        self.assertIn("COLLECTION_SUMMARY_MARKER", snapshot)
        self.assertIn("[section truncated]", snapshot)

    def test_analyzer_digest_never_exceeds_hard_serialized_ceiling(self):
        content = {
            "pr_metadata": {
                "number": 7,
                "title": "T" * 10000,
                "body": "B" * 100000,
            },
            "file_changes": [
                {
                    "file_path": f"src/module_{index}.py",
                    "change_type": "modified",
                    "additions": 100,
                    "deletions": 50,
                    "diff": "+" + (str(index) * 10000),
                }
                for index in range(120)
            ],
            "ci_cd_results": {
                "state": "failure",
                "check_runs": [
                    {
                        "name": f"check-{index}-" + ("n" * 1000),
                        "status": "completed",
                        "conclusion": "failure",
                        "output": {"summary": "s" * 10000},
                    }
                    for index in range(80)
                ],
            },
        }

        digest = build_route_plan_digest(
            "formatted " + ("P" * 200000),
            content,
            max_chars=2048,
        )
        serialized = json.dumps(
            digest,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

        self.assertLessEqual(len(serialized), 2048)
        self.assertTrue(digest["truncation"]["overall_compacted"])

    def test_analyzer_uses_structured_digest_and_absolute_deadline(self):
        client = _Client(
            [
                {
                    "reviewable_semantic_delta": True,
                    "minimum_evidence_boundary": "bounded_repo",
                    "complexity": "normal",
                    "reason": "Workflow behavior changed.",
                    "pr_type": "ci",
                    "risk_domains": [],
                    "verification_plan": [],
                }
            ]
        )
        deadline = Deadline.for_seconds(60)
        content = {
            "pr_metadata": {"number": 7, "title": "Tighten CI"},
            "file_changes": [{"file_path": ".github/workflows/ci.yml", "change_type": "modified", "diff": "+permissions: read-all"}],
            "ci_cd_results": {"state": "failure", "check_runs": [{"name": "test", "conclusion": "failure"}]},
        }
        result = analyze_pr_complexity("# formatted PR", pr_content=content, client=client, deadline=deadline)
        prompt = client.calls[0]["messages"][1]["content"]

        self.assertEqual(result["pr_type"], "ci")
        self.assertIs(client.calls[0]["deadline"], deadline)
        self.assertIn("llamapreview.route_digest.v3", prompt)
        self.assertIn("permissions: read-all", prompt)
        ci_digest = build_route_plan_digest("details", content)["ci"]
        self.assertEqual(ci_digest["commit_status_state"], "failure")
        self.assertEqual(ci_digest["aggregate_classification"], "failure")
        self.assertNotIn("state", ci_digest)

        mixed = dict(content)
        mixed["ci_cd_results"] = {
            "state": "success",
            "check_runs": [
                {"name": "test", "status": "completed", "conclusion": "failure"}
            ],
        }
        mixed_ci = build_route_plan_digest("details", mixed)["ci"]
        self.assertEqual(mixed_ci["commit_status_state"], "success")
        self.assertEqual(mixed_ci["aggregate_classification"], "failure")

    def test_analyzer_balances_changed_files_with_bounded_ci_diagnostic(self):
        annotation = {
            "path": ".github/workflows/release.yml",
            "start_line": 119,
            "end_line": 119,
            "annotation_level": "failure",
            "title": "Pin action",
            "message": "Use a full commit SHA. " + ("m" * 900),
        }
        content = {
            "pr_metadata": {"number": 7, "title": "Large workflow update"},
            "file_changes": [
                {
                    "file_path": f"src/module_{index}.py",
                    "change_type": "modified",
                    "additions": 5,
                    "deletions": 2,
                    "diff": "+" + ("x" * 5_000),
                }
                for index in range(50)
            ],
            "ci_cd_results": {
                "state": "success",
                "check_runs": [
                    {
                        "name": f"security-{index}",
                        "status": "completed",
                        "conclusion": "failure",
                        "details_url": f"https://example.test/{index}",
                        "output": {
                            "title": "Failure",
                            "summary": "Pin the action. " + ("s" * 1_900),
                            "text": "t" * 2_000,
                        },
                        "annotations": [dict(annotation) for _ in range(12)],
                    }
                    for index in range(8)
                ],
            },
        }

        digest = build_route_plan_digest("details", content, max_chars=60_000)
        serialized = json.dumps(
            digest,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

        self.assertLessEqual(len(serialized), 60_000)
        self.assertGreater(len(digest["files"]), 10)
        self.assertEqual(digest["ci"]["aggregate_classification"], "failure")
        self.assertTrue(
            any(item.get("annotations") for item in digest["ci"]["checks"])
        )

    def test_pr_truncation_preserves_one_generated_current_head_ci_block(self):
        block = (
            "<CURRENT_HEAD_CI_SNAPSHOT>\n"
            '{"aggregate_classification":"failure","checks":[{"name":"security","annotations":[{"path":"workflow.yml","start_line":9,"message":"Pin SHA"}]}]}\n'
            "</CURRENT_HEAD_CI_SNAPSHOT>"
        )
        value = ("prefix\n" * 30_000) + "\n" + block

        rendered = truncate_preserving_current_ci(value, 120_000)

        self.assertLessEqual(len(rendered), 120_000)
        self.assertEqual(rendered.count("<CURRENT_HEAD_CI_SNAPSHOT>"), 1)
        self.assertEqual(rendered.count("</CURRENT_HEAD_CI_SNAPSHOT>"), 1)
        self.assertIn('"message":"Pin SHA"', rendered)

    def test_post_route_plan_runs_after_inventory_and_binds_hit_evidence(self):
        inventory = _inventory("src/service.py")
        runtime = _Runtime({"src/service.py": "def run():\n    return True\n"})
        verification_step = {
            "question": "Inspect the changed service.",
            "why_it_matters": "It is the changed runtime file.",
            "tool": "read_file",
            "args": {"path": "src/service.py"},
        }
        client = _Client(
            [
                _plan_payload(verification_step),
                {
                    "summary": "The changed file was inspected.",
                    "answered": [{"question": "Inspect the changed service.", "evidence": "PR-head file content."}],
                    "unresolved_gaps": [],
                    "followups": [],
                    "complete": False,
                }
            ]
        )
        route_plan = {
            "complexity": "normal",
            "reason": "Contained code change.",
            "pr_type": "code",
            "risk_domains": [],
            "verification_plan": [verification_step],
        }
        with patch(
            "lambdas.LlamaPReviewPipeline.context_engine.initialization.get_repo_structure_for_llm",
            return_value=_tree_result(inventory),
        ), patch(
            "lambdas.LlamaPReviewPipeline.context_engine.pfr.orchestration.assemble_reconcile_context",
            wraps=assemble_reconcile_context,
        ) as assemble:
            context, meta = collect_context_pfr(
                runtime=runtime,
                github_token="token",
                repo_full_name="owner/repo",
                pr_content={"file_changes": [{"file_path": "src/service.py", "change_type": "modified"}]},
                pr_details="details",
                head_sha="head1234",
                default_branch="main",
                client=client,
                route_plan=route_plan,
                max_context_chars=5000,
            )

        self.assertEqual(
            [call["trace_phase"] for call in client.calls],
            ["pfr_plan", "pfr_reconcile"],
        )
        self.assertEqual(client.calls[1]["messages"][0]["role"], "system")
        self.assertIn("<UNTRUSTED_FETCHED_CONTEXT>", client.calls[1]["messages"][1]["content"])
        self.assertIn("## Verification Ledger", client.calls[1]["messages"][1]["content"])
        self.assertIn("## Collection Summary", client.calls[1]["messages"][1]["content"])
        self.assertTrue(
            any(call.kwargs.get("max_chars") == 180000 for call in assemble.call_args_list)
        )
        self.assertEqual(meta["pfr_plan_source"], "post_route_standalone_plan")
        self.assertTrue(meta["route_plan_lineage"]["plan_after_inventory"])
        self.assertEqual(meta["pfr_reconcile"]["answered"][0]["question"], "Inspect the changed service.")
        self.assertEqual(meta["evidence_ledger"]["resolutions"][0]["status"], "answered")
        self.assertFalse(meta["pfr_reconcile_representation_repairs"][0]["attempted"])
        self.assertLessEqual(len(context), 5000)

    def test_standalone_plan_non_object_root_uses_deterministic_floor(self):
        inventory = _inventory("src/service.py")
        runtime = _Runtime({"src/service.py": "def run():\n    return True\n"})
        client = _Client(
            [
                [],
                {
                    "summary": "The deterministic changed-file floor ran.",
                    "answered": [],
                    "unresolved_gaps": [],
                    "followups": [],
                    "complete": True,
                },
            ]
        )
        with patch(
            "lambdas.LlamaPReviewPipeline.context_engine.initialization.get_repo_structure_for_llm",
            return_value=_tree_result(inventory),
        ):
            _context, meta = collect_context_pfr(
                runtime=runtime,
                github_token="token",
                repo_full_name="owner/repo",
                pr_content={
                    "file_changes": [
                        {"file_path": "src/service.py", "change_type": "modified"}
                    ]
                },
                pr_details="details",
                head_sha="head1234",
                default_branch="main",
                client=client,
            )

        self.assertEqual(meta["pfr_plan_status"], "fallback_used")
        self.assertTrue(meta["plan_fallback_used"])
        self.assertIn(("owner/repo", "src/service.py", "head1234"), runtime.reads)
        self.assertEqual(
            [call["trace_phase"] for call in client.calls],
            ["pfr_plan", "pfr_reconcile"],
        )

    def test_all_invalid_model_plan_steps_get_postfilter_floor(self):
        inventory = _inventory("src/service.py")
        runtime = _Runtime({"src/service.py": "def run():\n    return True\n"})
        client = _Client(
            [
                {
                    "complexity": "normal",
                    "pr_type": "code",
                    "risk_domains": [],
                    "verification_plan": [
                        {
                            "question": "Inspect the changed service.",
                            "why_it_matters": "Runtime behavior changed.",
                            "tool": "download_repository",
                            "args": {},
                        }
                    ],
                },
                {
                    "summary": "The deterministic changed-file floor ran.",
                    "answered": [],
                    "unresolved_gaps": [],
                    "followups": [],
                    "complete": True,
                },
            ]
        )
        with patch(
            "lambdas.LlamaPReviewPipeline.context_engine.initialization.get_repo_structure_for_llm",
            return_value=_tree_result(inventory),
        ):
            _context, meta = collect_context_pfr(
                runtime=runtime,
                github_token="token",
                repo_full_name="owner/repo",
                pr_content={
                    "file_changes": [
                        {"file_path": "src/service.py", "change_type": "modified"}
                    ]
                },
                pr_details="details",
                head_sha="head1234",
                default_branch="main",
                client=client,
            )

        self.assertEqual(meta["pfr_plan_status"], "model_with_deterministic_floor")
        self.assertTrue(meta["deterministic_plan_floor_used"])
        self.assertIn(("owner/repo", "src/service.py", "head1234"), runtime.reads)

    def test_missing_comma_reconcile_is_locally_normalized(self):
        valid = json.dumps(
            {
                "summary": "The changed file was read.",
                "answered": [],
                "unresolved_gaps": [],
                "followups": [],
                "complete": True,
            }
        )
        malformed = valid.replace(', "answered"', ' "answered"', 1)
        client = _RawClient([malformed])

        result = _reconcile(
            client=client,
            model="deepseek-v4-pro",
            reasoning_effort="max",
            pr_details="details",
            plan={},
            context_text="context",
            trace_metadata={"repo": "owner/repo", "pr_number": 1},
            round_index=1,
            allow_representation_repair=True,
        )

        self.assertEqual(len(client.calls), 1)
        self.assertFalse(result["_representation_repair"]["attempted"])
        self.assertIn(
            "json_missing_comma_inserted",
            result["_representation_normalizations"],
        )

    def test_removed_semantic_roots_are_ignored_without_model_repair(self):
        payload = {
            "summary": "The fetched evidence remains exact.",
            "answered": [],
            "unresolved_gaps": [],
            "followups": [],
            "complete": True,
            "decision_obligations": [{"proposition": "obsolete"}],
            "obligation_dispositions": [{"status": "satisfied"}],
            "semantic_closure": {"version": 1},
        }

        client = _RawClient([payload])
        result = _reconcile(
            client=client,
            model="deepseek-v4-pro",
            reasoning_effort="max",
            pr_details="details",
            plan=_plan_payload(),
            context_text="context",
            trace_metadata={"repo": "owner/repo", "pr_number": 1},
            round_index=1,
            allow_representation_repair=True,
        )

        self.assertNotIn("decision_obligations", result)
        self.assertNotIn("obligation_dispositions", result)
        self.assertNotIn("semantic_closure", result)
        self.assertIn(
            "extra_root_fields_removed:3",
            result["_representation_normalizations"],
        )
        self.assertEqual(len(result["_model_usages"]), 1)
        self.assertFalse(result["_representation_repair"]["attempted"])
        request = client.calls[0]["messages"][1]["content"]
        self.assertNotIn("CRITICAL_CLOSURE_TARGETS", request)

    def test_locally_normalized_reconcile_repair_cannot_add_a_new_followup(self):
        initial = json.dumps(
            {
                "summary": "The changed file was read.",
                "answered": "not-an-array",
                "unresolved_gaps": [],
                "followups": [],
                "complete": False,
            }
        )
        malformed = initial.replace(', "answered"', ' "answered"', 1)
        repaired = json.dumps(
            {
                "summary": "The changed file was read.",
                "answered": [],
                "unresolved_gaps": [],
                "followups": [
                    {
                        "question": "Inspect a new path.",
                        "tool": "read_file",
                        "args": {"path": "src/new.py"},
                    }
                ],
                "complete": False,
            }
        )
        client = _RawClient([malformed, repaired])

        with self.assertRaises(PFRReconcileFailure) as caught:
            _reconcile(
                client=client,
                model="deepseek-v4-pro",
                reasoning_effort="max",
                pr_details="details",
                plan={},
                context_text="context",
                trace_metadata={"repo": "owner/repo", "pr_number": 1},
                round_index=1,
                allow_representation_repair=True,
            )

        self.assertEqual(caught.exception.kind, "repair_semantic_expansion")
        self.assertEqual(
            caught.exception.repair_telemetry["delta_guard_mode"],
            "action_scoped_monotonic",
        )

    def test_unrelated_collection_shape_repair_cannot_delete_valid_context(self):
        initial = {
            "summary": "The changed file was read.",
            "answered": "not-an-array",
            "unresolved_gaps": [
                {
                    "question_id": "q_ci",
                    "claim": "The head-SHA CI result is unavailable.",
                    "how_to_check": "Inspect the head-SHA CI run.",
                }
            ],
            "followups": [
                {
                    "question": "Read the direct caller.",
                    "tool": "read_file",
                    "args": {"path": "src/caller.py"},
                }
            ],
            "complete": False,
        }
        repaired = {
            "summary": initial["summary"],
            "answered": [],
            "unresolved_gaps": [],
            "followups": [],
            "complete": False,
        }
        client = _RawClient([initial, repaired])

        with self.assertRaises(PFRReconcileFailure) as caught:
            _reconcile(
                client=client,
                model="deepseek-v4-pro",
                reasoning_effort="max",
                pr_details="details",
                plan={},
                context_text="context",
                trace_metadata={"repo": "owner/repo", "pr_number": 1},
                round_index=1,
                allow_representation_repair=True,
            )

        self.assertEqual(caught.exception.kind, "repair_semantic_expansion")
        self.assertEqual(
            caught.exception.repair_telemetry["issue_codes"],
            ["field_type_invalid"],
        )

    def test_root_collection_repair_must_mark_reconcile_incomplete(self):
        initial = {
            "summary": "The changed file was read.",
            "answered": "not-an-array",
            "unresolved_gaps": [],
            "followups": [],
            "complete": True,
        }
        repaired = {**initial, "answered": [], "complete": False}

        accepted = _reconcile(
            client=_RawClient([initial, repaired]),
            model="deepseek-v4-pro",
            reasoning_effort="max",
            pr_details="details",
            plan={},
            context_text="context",
            trace_metadata={"repo": "owner/repo", "pr_number": 1},
            round_index=1,
            allow_representation_repair=True,
        )
        self.assertFalse(accepted["complete"])
        self.assertEqual(
            accepted["summary"], PFR_RECONCILE_NEUTRAL_SUMMARY
        )

        projected = _reconcile(
            client=_RawClient([initial, {**initial, "answered": []}]),
            model="deepseek-v4-pro",
            reasoning_effort="max",
            pr_details="details",
            plan={},
            context_text="context",
            trace_metadata={"repo": "owner/repo", "pr_number": 1},
            round_index=1,
            allow_representation_repair=True,
        )
        self.assertFalse(projected["complete"])
        self.assertEqual(
            projected["summary"], PFR_RECONCILE_NEUTRAL_SUMMARY
        )

    def test_evidence_binding_downgrade_neutralizes_summary_and_complete(self):
        state = CollectionState(
            pr_details="details",
            pr_content={},
            repo_full_name="owner/repo",
            head_sha="head1234",
            default_branch="main",
            runtime=_Runtime(),
        )
        question_id = state.evidence_ledger.register_question(
            question="Where is Worker used?",
            tool="search_code",
            args={"query": "Worker"},
        )
        state.evidence_ledger.record_event(
            question_id=question_id,
            tool="search_code",
            args={"query": "Worker"},
            outcome="no_hit",
        )

        reconciled = _apply_reconcile_to_ledger(
            {
                "summary": "All critical questions were verified from repository evidence.",
                "answered": [
                    {
                        "question_id": question_id,
                        "question": "Where is Worker used?",
                        "evidence": "Worker was verified.",
                    }
                ],
                "unresolved_gaps": [],
                "followups": [],
                "complete": True,
            },
            state,
        )

        self.assertEqual(reconciled["answered"], [])
        self.assertEqual(
            reconciled["summary"], PFR_RECONCILE_NEUTRAL_SUMMARY
        )
        self.assertFalse(reconciled["complete"])
        self.assertEqual(reconciled["unresolved_gaps"], [])

    def test_missing_unresolved_gaps_root_is_not_repairable(self):
        initial = {
            "summary": "The changed file was read.",
            "answered": [],
            "followups": [],
            "complete": True,
        }
        client = _RawClient(
            [
                initial,
                {**initial, "unresolved_gaps": [], "complete": False},
            ]
        )

        with self.assertRaises(PFRReconcileFailure) as caught:
            _reconcile(
                client=client,
                model="deepseek-v4-pro",
                reasoning_effort="max",
                pr_details="details",
                plan={},
                context_text="context",
                trace_metadata={"repo": "owner/repo", "pr_number": 1},
                round_index=1,
                allow_representation_repair=True,
            )

        self.assertEqual(len(client.calls), 1)
        self.assertEqual(
            caught.exception.repair_telemetry["skipped_reason"],
            "contract_not_repairable",
        )

    def test_invalid_followup_tool_contract_is_locally_dropped_without_losing_sibling(self):
        unresolved_gap = {
            "question_id": "q_presentation",
            "claim": "The changed presentation contract remains unverified.",
            "how_to_check": "Inspect the changed selectors at the PR head.",
            "evidence_refs": ["ev_changed"],
        }
        invalid_followup = {
            "question": "Inspect the changed selectors at the PR head.",
            "tool": "read_file",
            "args": {
                "path": "src/theme.css",
                "reason": "The changed selectors decide the open question.",
                "symbols": [
                    ".alpha",
                    ".beta",
                    ".gamma",
                    ".delta",
                    ".epsilon",
                    ".zeta",
                ],
            },
        }
        valid_followup = {
            "question": "Inspect the changed selector entry point.",
            "tool": "read_file",
            "args": {
                "path": "src/theme.css",
                "reason": "The changed entry point decides the open question.",
                "symbols": [".alpha"],
            },
        }
        initial = {
            "summary": "One bounded verification question remains.",
            "answered": [],
            "unresolved_gaps": [unresolved_gap],
            "followups": [invalid_followup, valid_followup],
            "complete": False,
        }
        client = _RawClient([initial])

        result = _reconcile(
            client=client,
            model="deepseek-v4-pro",
            reasoning_effort="max",
            pr_details="details",
            plan=_plan_payload(),
            context_text="context",
            trace_metadata={"repo": "owner/repo", "pr_number": 1},
            round_index=1,
            allow_representation_repair=True,
        )

        self.assertEqual(len(client.calls), 1)
        self.assertEqual(result["unresolved_gaps"], [unresolved_gap])
        self.assertEqual(result["followups"], [valid_followup])
        self.assertFalse(result["complete"])
        self.assertEqual(
            result["summary"], PFR_RECONCILE_NEUTRAL_SUMMARY
        )
        self.assertFalse(result["_representation_repair"]["attempted"])
        self.assertIn(
            "followups.tool_contract_invalid_dropped:0",
            result["_representation_normalizations"],
        )

    def test_malformed_unresolved_gap_cannot_be_deleted_by_repair(self):
        unknown = {
            "claim": "The changed behavior is not verified.",
        }
        initial = {
            "summary": "One merge-affecting uncertainty remains.",
            "answered": [],
            "unresolved_gaps": [unknown],
            "followups": [],
            "complete": False,
        }
        client = _RawClient([initial, {**initial, "unresolved_gaps": []}])

        with self.assertRaises(PFRReconcileFailure) as caught:
            _reconcile(
                client=client,
                model="deepseek-v4-pro",
                reasoning_effort="max",
                pr_details="details",
                plan={},
                context_text="context",
                trace_metadata={"repo": "owner/repo", "pr_number": 1},
                round_index=1,
                allow_representation_repair=True,
            )

        self.assertEqual(len(client.calls), 1)
        self.assertEqual(
            caught.exception.repair_telemetry["skipped_reason"],
            "contract_not_repairable",
        )

    def test_answer_missing_evidence_is_not_deleted_by_format_repair(self):
        valid_answer = {
            "question_id": "q_valid",
            "question": "Does the guard run?",
            "evidence_refs": ["ev_valid"],
            "evidence": "src/service.py:10 calls guard().",
        }
        bad_answer = {
            "question_id": "q_bad",
            "question": "Is the fallback covered?",
            "evidence_refs": ["ev_bad"],
            "evidence": "",
        }
        initial = {
            "summary": "Two verification questions were reconciled.",
            "answered": [valid_answer, bad_answer],
            "unresolved_gaps": [],
            "followups": [],
            "complete": False,
        }

        destructive_client = _RawClient([initial, {**initial, "answered": [valid_answer]}])
        with self.assertRaises(PFRReconcileFailure) as caught:
            _reconcile(
                client=destructive_client,
                model="deepseek-v4-pro",
                reasoning_effort="max",
                pr_details="details",
                plan={},
                context_text="context",
                trace_metadata={"repo": "owner/repo", "pr_number": 1},
                round_index=1,
                allow_representation_repair=True,
            )
        self.assertEqual(len(destructive_client.calls), 1)
        self.assertEqual(caught.exception.kind, "schema_validation_error")
        self.assertEqual(
            caught.exception.repair_telemetry["skipped_reason"],
            "contract_not_repairable",
        )

    def test_delete_item_cannot_rewrite_bad_answer_into_new_evidence(self):
        valid_answer = {
            "question_id": "q_valid",
            "question": "Does the guard run?",
            "evidence_refs": ["ev_valid"],
            "evidence": "src/service.py:10 calls guard().",
        }
        bad_answer = {
            "question_id": "q_bad",
            "question": "Is the fallback covered?",
            "evidence_refs": ["ev_bad"],
            "evidence": "",
        }
        invented_answer = {
            **bad_answer,
            "evidence": "A repair-only claim says the fallback is covered.",
        }
        initial = {
            "summary": "Two verification questions were reconciled.",
            "answered": [valid_answer, bad_answer],
            "unresolved_gaps": [],
            "followups": [],
            "complete": False,
        }
        client = _RawClient(
            [initial, {**initial, "answered": [valid_answer, invented_answer]}]
        )

        with self.assertRaises(PFRReconcileFailure) as caught:
            _reconcile(
                client=client,
                model="deepseek-v4-pro",
                reasoning_effort="max",
                pr_details="details",
                plan={},
                context_text="context",
                trace_metadata={"repo": "owner/repo", "pr_number": 1},
                round_index=1,
                allow_representation_repair=True,
            )

        self.assertEqual(len(client.calls), 1)
        self.assertEqual(caught.exception.kind, "schema_validation_error")
        self.assertEqual(
            caught.exception.repair_telemetry["skipped_reason"],
            "contract_not_repairable",
        )

    def test_cross_field_repair_must_preserve_followups_and_demote_complete(self):
        followup = {
            "question": "Read the direct caller.",
            "tool": "read_file",
            "args": {"path": "src/caller.py"},
        }
        initial = {
            "summary": "One material followup remains.",
            "answered": [],
            "unresolved_gaps": [],
            "followups": [followup],
            "complete": True,
        }

        accepted_client = _RawClient(
            [initial, {**initial, "complete": False}]
        )
        accepted = _reconcile(
            client=accepted_client,
            model="deepseek-v4-pro",
            reasoning_effort="max",
            pr_details="details",
            plan={},
            context_text="context",
            trace_metadata={"repo": "owner/repo", "pr_number": 1},
            round_index=1,
            allow_representation_repair=True,
        )
        self.assertFalse(accepted["complete"])
        self.assertEqual(
            accepted["followups"],
            [
                {
                    **followup,
                    "args": {
                        **followup["args"],
                        "reason": followup["question"],
                    },
                }
            ],
        )
        self.assertIn(
            "followups.args.reason:from_question",
            accepted["_representation_normalizations"],
        )

        destructive_client = _RawClient(
            [initial, {**initial, "followups": []}]
        )
        with self.assertRaises(PFRReconcileFailure) as caught:
            _reconcile(
                client=destructive_client,
                model="deepseek-v4-pro",
                reasoning_effort="max",
                pr_details="details",
                plan={},
                context_text="context",
                trace_metadata={"repo": "owner/repo", "pr_number": 1},
                round_index=1,
                allow_representation_repair=True,
            )
        self.assertEqual(caught.exception.kind, "repair_semantic_expansion")

    def test_reconcile_repair_fails_closed_when_canonical_issues_are_omitted(self):
        initial = {
            "summary": "Several malformed items were returned.",
            "answered": [None, {"question": "Missing evidence", "evidence": ""}],
            "unresolved_gaps": [],
            "followups": [
                None,
                {"question": "", "tool": "unsupported", "args": []},
            ],
            "complete": False,
        }
        client = _RawClient([initial])

        with self.assertRaises(PFRReconcileFailure) as caught:
            _reconcile(
                client=client,
                model="deepseek-v4-pro",
                reasoning_effort="max",
                pr_details="details",
                plan={},
                context_text="context",
                trace_metadata={"repo": "owner/repo", "pr_number": 1},
                round_index=1,
                allow_representation_repair=True,
            )

        self.assertEqual(len(client.calls), 1)
        self.assertFalse(caught.exception.repair_telemetry["attempted"])
        self.assertGreater(caught.exception.repair_telemetry["omitted_issue_count"], 0)
        self.assertEqual(
            caught.exception.repair_telemetry["skipped_reason"],
            "contract_not_repairable",
        )

    def test_reconcile_delta_rejects_unknown_root_action(self):
        baseline = {
            "summary": "The changed file was read.",
            "answered": [],
            "unresolved_gaps": [],
            "followups": [],
            "complete": False,
        }
        issue = ContractRepairIssue(
            code="unknown_contract_failure",
            location="$",
            message="An unknown root repair was requested.",
            repair_action="repair_contract_conservatively",
            priority=1,
        )
        selection = RepairIssueSelection(
            selected=(issue,),
            raw_candidate_count=1,
            candidate_count=1,
            omitted_count=0,
            merged_count=0,
            ineligible_count=0,
            omitted_issue_codes=(),
            selected_occurrences=(("$",),),
        )

        with self.assertRaises(PFRStructuredOutputError) as caught:
            _validate_reconcile_repair_delta(baseline, dict(baseline), selection)

        self.assertEqual(caught.exception.kind, "repair_semantic_expansion")

    def test_reconcile_delta_does_not_treat_nested_boolean_as_integer(self):
        followup = {
            "question": "List the changed directory.",
            "tool": "list_dir",
            "args": {"target_path": "src", "max_depth": 1},
        }
        initial = {
            "summary": "A followup remains.",
            "answered": "not-an-array",
            "unresolved_gaps": [],
            "followups": [followup],
            "complete": False,
        }
        rebound_followup = {
            **followup,
            "args": {"target_path": "src", "max_depth": True},
        }
        repaired = {
            **initial,
            "answered": [],
            "followups": [rebound_followup],
        }
        client = _RawClient([initial, repaired])

        with self.assertRaises(PFRReconcileFailure) as caught:
            _reconcile(
                client=client,
                model="deepseek-v4-pro",
                reasoning_effort="max",
                pr_details="details",
                plan={},
                context_text="context",
                trace_metadata={"repo": "owner/repo", "pr_number": 1},
                round_index=1,
                allow_representation_repair=True,
            )

        self.assertEqual(caught.exception.kind, "repair_semantic_expansion")

    def test_reconcile_extra_fields_are_stripped_symmetrically_before_delta(self):
        initial = {
            "summary": "A collection shape is malformed.",
            "answered": "not-an-array",
            "unresolved_gaps": [],
            "followups": [],
            "complete": False,
            "extra_marker": True,
        }
        repaired = {**initial, "answered": [], "extra_marker": 1}
        client = _RawClient([initial, repaired])

        accepted = _reconcile(
            client=client,
            model="deepseek-v4-pro",
            reasoning_effort="max",
            pr_details="details",
            plan={},
            context_text="context",
            trace_metadata={"repo": "owner/repo", "pr_number": 1},
            round_index=1,
            allow_representation_repair=True,
        )

        self.assertNotIn("extra_marker", accepted)
        self.assertEqual(accepted["answered"], [])
        self.assertIn(
            "extra_root_fields_removed:1",
            accepted["_representation_normalizations"],
        )

    def test_empty_array_reconcile_fails_closed_without_model_reconstruction(self):
        client = _RawClient(["[]"])

        with self.assertRaises(PFRReconcileFailure) as caught:
            _reconcile(
                client=client,
                model="deepseek-v4-pro",
                reasoning_effort="max",
                pr_details="details",
                plan={},
                context_text="context",
                trace_metadata={"repo": "owner/repo", "pr_number": 1},
                round_index=1,
                allow_representation_repair=True,
            )

        self.assertEqual(len(client.calls), 1)
        self.assertEqual(
            caught.exception.repair_telemetry["skipped_reason"],
            "representation_baseline_missing",
        )

    def test_single_object_array_reconcile_is_unwrapped_locally(self):
        content = json.dumps(
            [
                {
                    "summary": "The changed file was read.",
                    "answered": [],
                    "unresolved_gaps": [],
                    "followups": [],
                    "complete": True,
                }
            ]
        )
        client = _RawClient([content])

        result = _reconcile(
            client=client,
            model="deepseek-v4-pro",
            reasoning_effort="max",
            pr_details="details",
            plan={},
            context_text="context",
            trace_metadata={"repo": "owner/repo", "pr_number": 1},
            round_index=1,
            allow_representation_repair=True,
        )

        self.assertEqual(len(client.calls), 1)
        self.assertIn(
            "json_single_object_array_unwrapped",
            result["_representation_normalizations"],
        )

    def test_reconcile_shape_failure_gets_one_same_prefix_representation_repair(self):
        inventory = _inventory("src/service.py")
        runtime = _Runtime({"src/service.py": "def run():\n    return True\n"})
        verification_step = {
            "question": "Inspect the changed service.",
            "why_it_matters": "Runtime behavior changed.",
            "tool": "read_file",
            "args": {"path": "src/service.py"},
        }
        client = _Client(
            [
                _plan_payload(verification_step),
                {
                    "summary": 7,
                    "answered": "not-an-array",
                    "unresolved_gaps": [],
                    "followups": [],
                    "complete": "not-a-boolean",
                },
                {
                    "summary": PFR_RECONCILE_NEUTRAL_SUMMARY,
                    "answered": [],
                    "unresolved_gaps": [],
                    "followups": [],
                    "complete": False,
                },
            ]
        )
        route_plan = {
            "complexity": "normal",
            "pr_type": "code",
            "risk_domains": [],
            "verification_plan": [verification_step],
        }
        with patch(
            "lambdas.LlamaPReviewPipeline.context_engine.initialization.get_repo_structure_for_llm",
            return_value=_tree_result(inventory),
        ):
            _context, meta = collect_context_pfr(
                runtime=runtime,
                github_token="token",
                repo_full_name="owner/repo",
                pr_content={
                    "file_changes": [
                        {"file_path": "src/service.py", "change_type": "modified"}
                    ]
                },
                pr_details="details",
                head_sha="head1234",
                default_branch="main",
                client=client,
                route_plan=route_plan,
            )

        self.assertEqual(
            [call["trace_phase"] for call in client.calls],
            ["pfr_plan", "pfr_reconcile", "pfr_reconcile_representation_repair"],
        )
        self.assertEqual(
            client.calls[2]["messages"][: len(client.calls[1]["messages"])],
            client.calls[1]["messages"],
        )
        self.assertFalse(client.calls[2]["thinking"])
        self.assertEqual(client.calls[2]["response_format"], {"type": "json_object"})
        self.assertEqual(client.calls[2]["max_tokens"], 12000)
        self.assertEqual(client.calls[2]["timeout_seconds"], 120.0)
        contract = meta["pfr_reconcile_representation_repairs"][0]
        self.assertTrue(contract["attempted"])
        self.assertTrue(contract["recovered"])
        self.assertEqual(contract["issue_count"], 1)
        self.assertEqual(contract["delta_guard_mode"], "action_scoped_monotonic")
        self.assertEqual(len(meta["pfr_reconcile_usages"]), 2)
        self.assertEqual(
            meta["pfr_reconcile"]["summary"], PFR_RECONCILE_NEUTRAL_SUMMARY
        )

    def test_reconcile_representation_repair_is_capped_once_across_all_rounds(self):
        inventory = _inventory("src/service.py")
        runtime = _Runtime({"src/service.py": "def run():\n    return True\n"})
        verification_step = {
            "question": "Inspect the changed service.",
            "why_it_matters": "Runtime behavior changed.",
            "tool": "read_file",
            "args": {"path": "src/service.py"},
        }
        followup = {
            "question": "Reread the changed service.",
            "tool": "read_file",
            "args": {"path": "src/service.py", "reason": "Confirm the file."},
        }
        client = _Client(
            [
                _plan_payload(verification_step),
                {
                    "summary": "More evidence is needed.",
                    "answered": "invalid",
                    "unresolved_gaps": [],
                    "followups": [followup],
                    "complete": "invalid",
                },
                {
                    "summary": "More evidence is needed.",
                    "answered": [],
                    "unresolved_gaps": [],
                    "followups": [followup],
                    "complete": False,
                },
                {
                    "summary": 9,
                    "answered": "invalid-again",
                    "unresolved_gaps": [],
                    "followups": [],
                    "complete": "invalid-again",
                },
            ]
        )
        route_plan = {
            "complexity": "normal",
            "pr_type": "code",
            "risk_domains": [],
            "verification_plan": [verification_step],
        }
        with patch(
            "lambdas.LlamaPReviewPipeline.context_engine.initialization.get_repo_structure_for_llm",
            return_value=_tree_result(inventory),
        ):
            _context, meta = collect_context_pfr(
                runtime=runtime,
                github_token="token",
                repo_full_name="owner/repo",
                pr_content={
                    "file_changes": [
                        {"file_path": "src/service.py", "change_type": "modified"}
                    ]
                },
                pr_details="details",
                head_sha="head1234",
                default_branch="main",
                client=client,
                route_plan=route_plan,
            )

        self.assertEqual(
            [call["trace_phase"] for call in client.calls],
            [
                "pfr_plan",
                "pfr_reconcile",
                "pfr_reconcile_representation_repair",
                "pfr_reconcile",
            ],
        )
        self.assertEqual(
            sum(bool(item.get("attempted")) for item in meta["pfr_reconcile_representation_repairs"]),
            1,
        )
        self.assertEqual(
            meta["pfr_reconcile_failures"][-1]["kind"],
            "schema_validation_error",
        )

    def test_initial_reconcile_http_failure_is_typed_in_pfr_telemetry(self):
        inventory = _inventory("src/service.py")

        class _HTTPFailingClient:
            def __init__(self):
                self.calls = []

            def chat(self, messages, **kwargs):
                self.calls.append({"messages": messages, **kwargs})
                if kwargs.get("trace_phase") == "pfr_plan":
                    return {
                        "choices": [
                            {
                                "message": {
                                    "role": "assistant",
                                    "content": json.dumps(_plan_payload()),
                                },
                                "finish_reason": "stop",
                            }
                        ],
                        "usage": {"total_tokens": 7},
                    }
                raise DeepSeekHTTPError("busy", status_code=503)

        client = _HTTPFailingClient()
        with patch(
            "lambdas.LlamaPReviewPipeline.context_engine.initialization.get_repo_structure_for_llm",
            return_value=_tree_result(inventory),
        ):
            _context, meta = collect_context_pfr(
                runtime=_Runtime({"src/service.py": "pass\n"}),
                github_token="token",
                repo_full_name="owner/repo",
                pr_content={
                    "file_changes": [
                        {"file_path": "src/service.py", "change_type": "modified"}
                    ]
                },
                pr_details="details",
                head_sha="head1234",
                default_branch="main",
                client=client,
                route_plan={
                    "complexity": "normal",
                    "pr_type": "code",
                    "risk_domains": [],
                    "verification_plan": [],
                },
            )

        self.assertEqual(
            [call["trace_phase"] for call in client.calls],
            ["pfr_plan", "pfr_reconcile"],
        )
        self.assertEqual(
            meta["pfr_reconcile_failures"],
            [{"round": 1, "kind": "model_http_error", "repair_attempted": False}],
        )
        self.assertEqual(meta["pfr_reconcile_representation_repairs"], [])

    def test_reconcile_delta_uses_normalized_boolean_baseline_during_repair(self):
        inventory = _inventory("src/service.py")
        client = _Client(
            [
                _plan_payload(),
                {
                    "summary": "The read completed.",
                    "answered": "invalid",
                    "unresolved_gaps": [],
                    "followups": [],
                    "complete": "true",
                },
                {
                    "summary": "The read completed.",
                    "answered": [],
                    "unresolved_gaps": [],
                    "followups": [],
                    "complete": False,
                },
            ]
        )
        with patch(
            "lambdas.LlamaPReviewPipeline.context_engine.initialization.get_repo_structure_for_llm",
            return_value=_tree_result(inventory),
        ):
            _context, meta = collect_context_pfr(
                runtime=_Runtime({"src/service.py": "pass\n"}),
                github_token="token",
                repo_full_name="owner/repo",
                pr_content={
                    "file_changes": [
                        {"file_path": "src/service.py", "change_type": "modified"}
                    ]
                },
                pr_details="details",
                head_sha="head1234",
                default_branch="main",
                client=client,
                route_plan={
                    "complexity": "normal",
                    "pr_type": "code",
                    "risk_domains": [],
                    "verification_plan": [],
                },
            )

        self.assertTrue(meta["pfr_reconcile_representation_repairs"][0]["recovered"])
        self.assertFalse(meta["pfr_reconcile"]["complete"])
        self.assertIn(
            "complete:string_boolean",
            meta["pfr_reconcile_representation_normalizations"][0]["repairs"],
        )

    def test_reconcile_repair_truncation_preserves_finish_reason_and_typed_failure(self):
        inventory = _inventory("src/service.py")

        class _MixedResponseClient:
            def __init__(self):
                self.calls = []
                self.responses = [
                    {
                        "choices": [
                            {
                                "message": {
                                    "role": "assistant",
                                    "content": json.dumps(_plan_payload()),
                                },
                                "finish_reason": "stop",
                            }
                        ],
                        "usage": {"total_tokens": 7},
                    },
                    {
                        "choices": [
                            {
                                "message": {
                                    "role": "assistant",
                                    "content": json.dumps(
                                        {
                                            "summary": "Needs a shape repair.",
                                            "answered": "invalid",
                                            "unresolved_gaps": [],
                                            "followups": [],
                                            "complete": False,
                                        }
                                    ),
                                },
                                "finish_reason": "stop",
                            }
                        ],
                        "usage": {"total_tokens": 10},
                    },
                    {
                        "choices": [
                            {
                                "message": {
                                    "role": "assistant",
                                    "content": '{"summary":"truncated"',
                                },
                                "finish_reason": "length",
                            }
                        ],
                        "usage": {"total_tokens": 3},
                    },
                ]

            def chat(self, messages, **kwargs):
                self.calls.append({"messages": messages, **kwargs})
                return self.responses.pop(0)

        client = _MixedResponseClient()
        with patch(
            "lambdas.LlamaPReviewPipeline.context_engine.initialization.get_repo_structure_for_llm",
            return_value=_tree_result(inventory),
        ):
            _context, meta = collect_context_pfr(
                runtime=_Runtime({"src/service.py": "pass\n"}),
                github_token="token",
                repo_full_name="owner/repo",
                pr_content={
                    "file_changes": [
                        {"file_path": "src/service.py", "change_type": "modified"}
                    ]
                },
                pr_details="details",
                head_sha="head1234",
                default_branch="main",
                client=client,
                route_plan={
                    "complexity": "normal",
                    "pr_type": "code",
                    "risk_domains": [],
                    "verification_plan": [],
                },
            )

        self.assertEqual(
            meta["pfr_reconcile_representation_repairs"][0]["repair_failure_kind"],
            "output_truncated",
        )
        self.assertEqual(
            meta["pfr_reconcile_finish_reasons"][0][
                "pfr_reconcile_representation_repair"
            ],
            "length",
        )
        self.assertEqual(meta["pfr_reconcile_failures"][0]["kind"], "output_truncated")

    def test_reconcile_string_complete_is_repaired_and_materiality_is_dropped(self):
        inventory = _inventory("src/service.py")
        client = _Client(
            [
                _plan_payload(),
                {
                    "summary": "Manual validation remains optional.",
                    "answered": [],
                    "unresolved_gaps": [
                        {
                            "claim": "Could not verify the optional smoke test.",
                            "how_to_check": "Run the smoke test if desired.",
                            "affects_merge": "false",
                        }
                    ],
                    "followups": [],
                    "complete": "true",
                }
            ]
        )
        with patch(
            "lambdas.LlamaPReviewPipeline.context_engine.initialization.get_repo_structure_for_llm",
            return_value=_tree_result(inventory),
        ):
            _context, meta = collect_context_pfr(
                runtime=_Runtime({"src/service.py": "pass\n"}),
                github_token="token",
                repo_full_name="owner/repo",
                pr_content={
                    "file_changes": [
                        {"file_path": "src/service.py", "change_type": "modified"}
                    ]
                },
                pr_details="details",
                head_sha="head1234",
                default_branch="main",
                client=client,
                route_plan={
                    "complexity": "normal",
                    "pr_type": "code",
                    "risk_domains": [],
                    "verification_plan": [],
                },
            )

        self.assertEqual(
            [call["trace_phase"] for call in client.calls],
            ["pfr_plan", "pfr_reconcile"],
        )
        self.assertFalse(meta["pfr_reconcile_representation_repairs"][0]["attempted"])
        self.assertEqual(
            meta["pfr_reconcile_representation_repairs"][0]["delta_guard_mode"],
            "not_applicable",
        )
        self.assertTrue(meta["pfr_reconcile"]["complete"])
        self.assertNotIn(
            "affects_merge",
            meta["pfr_reconcile"]["unresolved_gaps"][0],
        )
        repairs = meta["pfr_reconcile_representation_normalizations"][0]["repairs"]
        self.assertIn("complete:string_boolean", repairs)
        self.assertIn("extra_unresolved_gap_fields_removed:1", repairs)

    def test_reconcile_gap_without_materiality_is_valid_factual_output(self):
        inventory = _inventory("src/service.py")
        client = _Client(
            [
                _plan_payload(),
                {
                    "summary": "One uncertainty remains.",
                    "answered": [],
                    "unresolved_gaps": [
                        {
                            "claim": "Could not verify the changed behavior.",
                            "how_to_check": "Run the focused behavior test.",
                        }
                    ],
                    "followups": [],
                    "complete": True,
                }
            ]
        )
        with patch(
            "lambdas.LlamaPReviewPipeline.context_engine.initialization.get_repo_structure_for_llm",
            return_value=_tree_result(inventory),
        ):
            _context, meta = collect_context_pfr(
                runtime=_Runtime({"src/service.py": "pass\n"}),
                github_token="token",
                repo_full_name="owner/repo",
                pr_content={
                    "file_changes": [
                        {"file_path": "src/service.py", "change_type": "modified"}
                    ]
                },
                pr_details="details",
                head_sha="head1234",
                default_branch="main",
                client=client,
                route_plan={
                    "complexity": "normal",
                    "pr_type": "code",
                    "risk_domains": [],
                    "verification_plan": [],
                },
            )

        self.assertEqual(
            [call["trace_phase"] for call in client.calls],
            ["pfr_plan", "pfr_reconcile"],
        )
        self.assertEqual(
            meta["pfr_reconcile"]["unresolved_gaps"][0]["claim"],
            "Could not verify the changed behavior.",
        )
        self.assertNotIn(
            "affects_merge",
            meta["pfr_reconcile"]["unresolved_gaps"][0],
        )
        self.assertEqual(
            meta["pfr_terminal_reconcile_failure_kind"],
            "",
        )
        repair = meta["pfr_reconcile_representation_repairs"][0]
        self.assertFalse(repair["attempted"])
        self.assertIsNone(repair["trigger_kind"])

    def test_reconcile_repair_cannot_add_a_new_unresolved_gap(self):
        inventory = _inventory("src/service.py")
        client = _Client(
            [
                _plan_payload(),
                {
                    "summary": "The changed file was fetched.",
                    "answered": "invalid",
                    "unresolved_gaps": [],
                    "followups": [],
                    "complete": "invalid",
                },
                {
                    "summary": "The changed file was fetched.",
                    "answered": [],
                    "unresolved_gaps": [
                        {
                            "claim": "A new repair-only uncertainty.",
                            "how_to_check": "Perform a new investigation.",
                        }
                    ],
                    "followups": [],
                    "complete": True,
                },
            ]
        )
        with patch(
            "lambdas.LlamaPReviewPipeline.context_engine.initialization.get_repo_structure_for_llm",
            return_value=_tree_result(inventory),
        ):
            _context, meta = collect_context_pfr(
                runtime=_Runtime({"src/service.py": "pass\n"}),
                github_token="token",
                repo_full_name="owner/repo",
                pr_content={
                    "file_changes": [
                        {"file_path": "src/service.py", "change_type": "modified"}
                    ]
                },
                pr_details="details",
                head_sha="head1234",
                default_branch="main",
                client=client,
                route_plan={
                    "complexity": "normal",
                    "pr_type": "code",
                    "risk_domains": [],
                    "verification_plan": [],
                },
            )

        self.assertEqual(
            [call["trace_phase"] for call in client.calls],
            ["pfr_plan", "pfr_reconcile", "pfr_reconcile_representation_repair"],
        )
        self.assertFalse(meta["pfr_reconcile_representation_repairs"][0]["recovered"])
        self.assertEqual(
            meta["pfr_reconcile_representation_repairs"][0]["repair_failure_kind"],
            "repair_semantic_expansion",
        )
        self.assertEqual(meta["pfr_reconcile"]["answered"], [])
        self.assertEqual(meta["pfr_reconcile"]["unresolved_gaps"], [])
        self.assertFalse(meta["pfr_reconcile"]["complete"])
        self.assertFalse(meta["pfr_terminal_reconcile_available"])
        self.assertTrue(meta["pfr_direct_evidence_only"])
        self.assertEqual(
            meta["pfr_terminal_reconcile_failure_kind"],
            "repair_semantic_expansion",
        )
        self.assertTrue(meta["pfr_reconcile_failures"])

    def test_search_no_hit_is_downgraded_from_reconcile_answer(self):
        inventory = _inventory("src/service.py")
        verification_step = {
            "question": "Where is Widget used?",
            "why_it_matters": "Caller impact matters.",
            "tool": "search_code",
            "args": {"query": "Widget(", "reason": "Find callers."},
        }
        client = _Client(
            [
                _plan_payload(verification_step),
                {
                    "summary": "No usages found.",
                    "answered": [
                        {
                            "question_id": "q_fabricated",
                            "question": "Where is Widget used?",
                            "evidence_refs": ["ev_fabricated"],
                            "evidence": "Search returned no hits.",
                        }
                    ],
                    "unresolved_gaps": [],
                    "followups": [],
                    "complete": True,
                }
            ]
        )
        route_plan = {
            "complexity": "normal",
            "pr_type": "code",
            "risk_domains": [],
            "verification_plan": [verification_step],
        }
        with patch(
            "lambdas.LlamaPReviewPipeline.context_engine.initialization.get_repo_structure_for_llm",
            return_value=_tree_result(inventory),
        ):
            _context, meta = collect_context_pfr(
                runtime=_Runtime(),
                github_token="token",
                repo_full_name="owner/repo",
                pr_content={"file_changes": [{"file_path": "README.md", "diff": "+docs"}]},
                pr_details="details",
                head_sha="head1234",
                default_branch="main",
                client=client,
                route_plan=route_plan,
                max_search_calls=1,
            )

        self.assertEqual(meta["search_calls"], 1)
        self.assertEqual(meta["search_hits"], 0)
        self.assertEqual(meta["pfr_reconcile"]["answered"], [])
        self.assertEqual(meta["pfr_reconcile"]["unresolved_gaps"], [])
        resolution = next(item for item in meta["evidence_ledger"]["resolutions"] if item["question_id"])
        self.assertEqual(resolution["status"], "unknown")
        self.assertNotIn("ev_fabricated", resolution["evidence_refs"])
        self.assertFalse(meta["pfr_reconcile_representation_repairs"][0]["attempted"])

    def test_empty_reused_high_route_gets_deterministic_changed_file_floor(self):
        inventory = _inventory("src/service.py")
        runtime = _Runtime({"src/service.py": "def run():\n    return True\n"})
        client = _Client(
            [
                _plan_payload(),
                {
                    "summary": "Changed file read.",
                    "answered": [],
                    "unresolved_gaps": [],
                    "followups": [],
                    "complete": True,
                },
            ]
        )
        with patch(
            "lambdas.LlamaPReviewPipeline.context_engine.initialization.get_repo_structure_for_llm",
            return_value=_tree_result(inventory),
        ):
            _context, meta = collect_context_pfr(
                runtime=runtime,
                github_token="token",
                repo_full_name="owner/repo",
                pr_content={"file_changes": [{"file_path": "src/service.py", "change_type": "modified"}]},
                pr_details="details",
                head_sha="head1234",
                default_branch="main",
                client=client,
                route_plan={"complexity": "high", "pr_type": "mixed", "risk_domains": [], "verification_plan": []},
            )

        self.assertTrue(meta["deterministic_plan_floor_used"])
        self.assertIn("src/service.py", meta["read_success_paths"])
        self.assertEqual(
            [call["trace_phase"] for call in client.calls],
            ["pfr_plan", "pfr_reconcile"],
        )

    def test_normal_plan_cap_is_not_reported_as_runtime_health_failure(self):
        state = CollectionState(
            pr_details="details",
            pr_content={"file_changes": []},
            repo_full_name="owner/repo",
            head_sha="head1234",
            default_branch="main",
            runtime=_Runtime(),
            repo_inventory=_inventory("a.py", "b.py", "c.py"),
            accessible_files={"a.py", "b.py", "c.py"},
        )
        # A real final context should expose actual calls/hits separately.
        state.search_calls = 2
        state.tool_events = [
            {"tool": "search_code", "outcome": "no_hit", "hit_count": 0, "args": {}, "paths": []},
            {"tool": "search_code", "outcome": "hit", "hit_count": 1, "args": {}, "paths": ["a.py"]},
        ]
        meta = context_meta(state)

        self.assertEqual(meta["search_calls"], 2)
        self.assertEqual(meta["search_hits"], 1)
        self.assertNotIn("planned_batch_capped", meta["budget_health_reasons"])
        self.assertLessEqual(len(assemble_context(state)), state.max_context_chars)

    def test_reconcile_event_index_is_complete_when_diagnostic_context_truncates(self):
        state = CollectionState(
            pr_details="details",
            pr_content={"file_changes": []},
            repo_full_name="owner/repo",
            head_sha="head1234",
            default_branch="main",
            runtime=_Runtime(),
            repo_inventory=_inventory("src/service.py"),
            accessible_files={"src/service.py"},
        )
        expected_question_ids = []
        expected_event_ids = []
        for index in range(25):
            question_id = state.evidence_ledger.register_question(
                question=f"Verify behavior {index}.",
                tool="read_file",
                args={"path": "src/service.py", "symbols": [f"Symbol{index}"]},
            )
            event_id = state.evidence_ledger.record_event(
                question_id=question_id,
                tool="read_file",
                args={"path": "src/service.py", "symbols": [f"Symbol{index}"]},
                outcome="hit",
                paths=["src/service.py"],
                source_ref="pr_head:head1234",
                coverage_type="file_slice",
                observed_state="content_observed",
            )
            expected_question_ids.append(question_id)
            expected_event_ids.append(event_id)
            state.tool_events.append(
                {
                    "tool": "read_file",
                    "outcome": "hit",
                    "hit_count": 1,
                    "paths": ["src/service.py"],
                    "args": {"path": "src/service.py"},
                    "result_summary": "x" * 12000,
                    "question_id": question_id,
                    "evidence_event_id": event_id,
                    "metadata": {"coverage_type": "file_slice"},
                }
            )

        context, envelope, index_meta = assemble_reconcile_context(
            state,
            max_chars=180000,
        )

        self.assertTrue(index_meta["complete"])
        self.assertEqual(index_meta["event_count"], 25)
        self.assertLessEqual(len(context) + len(envelope) + 2, 180000)
        for identity in [*expected_question_ids, *expected_event_ids]:
            self.assertIn(identity, envelope)
        index_payload = json.loads(
            envelope.split("\n", 1)[1].rsplit("\n", 1)[0]
        )
        self.assertNotIn(
            "obligation_evidence_capabilities",
            index_payload,
        )
        self.assertEqual(
            [item["question_id"] for item in index_payload["questions"]],
            expected_question_ids,
        )
        self.assertEqual(
            [item["event_id"] for item in index_payload["events"]],
            expected_event_ids,
        )
        self.assertIn("[tool trace truncated]", context)

        client = _Client(
            [
                {
                    "summary": "Evidence identities remain available.",
                    "answered": [],
                    "unresolved_gaps": [],
                    "followups": [],
                    "complete": True,
                },
                {
                    "summary": "Evidence identities remain available.",
                    "answered": [],
                    "unresolved_gaps": [],
                    "followups": [],
                    "complete": True,
                },
            ]
        )
        for round_index in (1, 2):
            _reconcile(
                client=client,
                model="deepseek-chat",
                reasoning_effort="low",
                pr_details="details",
                plan={"verification_plan": []},
                context_text=context,
                evidence_index_envelope=envelope,
                trace_metadata={},
                round_index=round_index,
                allow_representation_repair=False,
            )
        for call in client.calls:
            user_content = call["messages"][1]["content"]
            for identity in [*expected_question_ids, *expected_event_ids]:
                self.assertIn(identity, user_content)

    def test_incomplete_reconcile_event_index_skips_model_and_uses_direct_evidence_only(self):
        inventory = _inventory("src/service.py")
        runtime = _Runtime(
            {"src/service.py": "def run():\n    return True\n"}
        )
        client = _Client(
            [
                _plan_payload(
                    {
                        "question": "Inspect the changed service.",
                        "why_it_matters": "It is the changed runtime file.",
                        "tool": "read_file",
                        "args": {"path": "src/service.py"},
                    }
                )
            ]
        )
        route_plan = {
            "complexity": "normal",
            "reason": "Contained code change.",
            "pr_type": "code",
            "risk_domains": [],
            "verification_plan": [
                {
                    "question": "Inspect the changed service.",
                    "why_it_matters": "It is the changed runtime file.",
                    "tool": "read_file",
                    "args": {"path": "src/service.py"},
                }
            ],
        }
        with patch(
            "lambdas.LlamaPReviewPipeline.context_engine.initialization.get_repo_structure_for_llm",
            return_value=_tree_result(inventory),
        ), patch(
            "lambdas.LlamaPReviewPipeline.context_engine.pfr.orchestration.assemble_reconcile_context",
            return_value=(
                "",
                "<CODE_GENERATED_EVIDENCE_EVENT_INDEX>x</CODE_GENERATED_EVIDENCE_EVENT_INDEX>",
                {"event_count": 1, "complete": False},
            ),
        ):
            _context, meta = collect_context_pfr(
                runtime=runtime,
                github_token="token",
                repo_full_name="owner/repo",
                pr_content={
                    "file_changes": [
                        {
                            "file_path": "src/service.py",
                            "change_type": "modified",
                        }
                    ]
                },
                pr_details="details",
                head_sha="head1234",
                default_branch="main",
                client=client,
                route_plan=route_plan,
            )

        self.assertEqual(
            [call["trace_phase"] for call in client.calls],
            ["pfr_plan"],
        )
        self.assertFalse(meta["pfr_evidence_index_complete"])
        self.assertEqual(meta["pfr_evidence_index_event_count"], 1)
        self.assertTrue(meta["pfr_direct_evidence_only"])
        self.assertFalse(meta["pfr_terminal_reconcile_available"])
        self.assertEqual(
            meta["pfr_terminal_reconcile_failure_kind"],
            "evidence_index_incomplete",
        )

    def test_flat_search_plan_steps_are_promoted_once_and_strictly_executed(self):
        inventory = _inventory("src/service.py")
        runtime = _Runtime(search_results=[])
        plan_steps = [
            {
                "question": f"Find exact caller {token}.",
                "why_it_matters": "A caller could change merge readiness.",
                "tool": "search_code",
                "query": token,
                "reason": "Find an exact current-head caller.",
                "intent": "external_usage",
            }
            for token in ("AlphaCall", "BetaCall", "GammaCall")
        ]
        client = _Client(
            [
                _plan_payload(*plan_steps),
                {
                    "summary": "Literal searches completed without hits.",
                    "answered": [],
                    "unresolved_gaps": [],
                    "followups": [],
                    "complete": True,
                }
            ]
        )
        route_plan = {
            "complexity": "normal",
            "reason": "Caller checks are useful.",
            "pr_type": "code",
            "risk_domains": [],
            "verification_plan": plan_steps,
        }
        with patch(
            "lambdas.LlamaPReviewPipeline.context_engine.initialization.get_repo_structure_for_llm",
            return_value=_tree_result(inventory),
        ):
            _context, meta = collect_context_pfr(
                runtime=runtime,
                github_token="token",
                repo_full_name="owner/repo",
                pr_content={
                    "file_changes": [
                        {
                            "file_path": "src/service.py",
                            "change_type": "modified",
                            "diff": (
                                "+AlphaCall()\n+BetaCall()\n+GammaCall()\n"
                            ),
                        }
                    ]
                },
                pr_details="details",
                head_sha="head1234",
                default_branch="main",
                client=client,
                route_plan=route_plan,
                max_search_calls=3,
            )

        self.assertEqual(len(runtime.searches), 3)
        self.assertEqual(
            meta["tool_arg_repair_counts"]["search_code.args_from_top_level"],
            3,
        )
        self.assertNotIn(
            "invalid_plan",
            meta["pfr_fetch_degradation_reason_counts"],
        )

    def test_reconcile_followups_use_the_same_flat_envelope_normalizer(self):
        normalized, actions = _normalize_reconcile_contract(
            {
                "summary": "One bounded follow-up can answer the question.",
                "answered": [],
                "unresolved_gaps": [],
                "followups": [
                    {
                        "question": "Find exact callers.",
                        "tool": "search_code",
                        "query": "WidgetFactory(",
                        "reason": "A caller could change merge readiness.",
                        "intent": "external_usage",
                    },
                    {
                        "question": "Inspect the exact implementation.",
                        "tool": "read_file",
                        "path": "src/service.py",
                        "reason": "The implementation determines behavior.",
                        "symbols": ["WidgetFactory"],
                    },
                    {
                        "question": "Inspect the bounded module.",
                        "tool": "list_dir",
                        "target_path": "src",
                        "max_depth": 2,
                        "reason": "The module inventory narrows follow-up.",
                    },
                ],
                "complete": False,
            }
        )

        self.assertEqual(
            [item["args"] for item in normalized["followups"]],
            [
                {
                    "query": "WidgetFactory(",
                    "reason": "A caller could change merge readiness.",
                    "intent": "external_usage",
                },
                {
                    "path": "src/service.py",
                    "reason": "The implementation determines behavior.",
                    "symbols": ["WidgetFactory"],
                },
                {
                    "target_path": "src",
                    "max_depth": 2,
                    "reason": "The module inventory narrows follow-up.",
                },
            ],
        )
        self.assertIn(
            "followups.search_code.args_from_top_level:1",
            actions,
        )
        self.assertIn(
            "followups.read_file.args_from_top_level:1",
            actions,
        )
        self.assertIn(
            "followups.list_dir.args_from_top_level:1",
            actions,
        )

        conflicted, conflict_actions = _normalize_reconcile_contract(
            {
                "summary": "Malformed follow-up.",
                "answered": [],
                "unresolved_gaps": [],
                "followups": [
                    {
                        "question": "Find callers.",
                        "tool": "search_code",
                        "args": {
                            "query": "WidgetFactory",
                            "reason": "Find callers.",
                        },
                        "query": "OtherWidget",
                    }
                ],
                "complete": False,
            }
        )
        self.assertIsNone(conflicted["followups"][0]["args"])
        self.assertIn(
            "followups.tool_envelope_invalid:conflicting_tool_arg_envelopes",
            conflict_actions,
        )


if __name__ == "__main__":
    unittest.main()
