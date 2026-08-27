import unittest
import inspect
from types import SimpleNamespace

from tests.unit.fakes import ensure_repo_root_on_path, set_default_env

ensure_repo_root_on_path()
set_default_env()

from lambdas.LlamaPReviewPipeline.context_engine.assembler import (
    assemble_context,
    assemble_reconcile_context,
    assemble_review_context,
)
from lambdas.LlamaPReviewPipeline.context_engine.pfr import (
    PLAN_CONTINUATION_PROMPT,
    PLAN_METHOD_PROMPT,
    PLAN_PROMPT,
    RECONCILE_SYSTEM_PROMPT,
    _append_pfr_sections,
    _normalize_author_acceptance_criteria,
    _strip_reconcile_extra_fields,
)
from lambdas.LlamaPReviewPipeline.context_engine.repo_structure import (
    RepoInventory,
)
from lambdas.LlamaPReviewPipeline.context_engine.state import CollectionState
from lambdas.LlamaPReviewPipeline.context_engine.tool_contract import (
    validate_verification_plan,
)
from lambdas.LlamaPReviewPipeline.review.analyzer import (
    _validate_route_contract,
    derive_unique_suffix_path_candidates,
)


def _pr_content(*, diff='+path = "example_files/asset.json"\n'):
    return {
        "pr_metadata": {"number": 74, "head_sha": "head123456789"},
        "file_changes": [
            {
                "file_path": "src/changed.py",
                "change_type": "modified",
                "diff": diff,
            }
        ],
    }


class PfrEvidenceContractTest(unittest.TestCase):
    def _assert_plan_priority_order(self, prompt):
        normalized = " ".join(prompt.casefold().split())
        priority_fragments = (
            "first verify concrete author acceptance criteria from the pr "
            "description or explicitly linked issue or acceptance material "
            "already supplied in the pr details",
            "second, when the pr changes tests, ci, or validation "
            "infrastructure, verify the authoritative runner, discovery "
            "configuration, workflow, or entrypoint",
            "third verify the highest-consequence locally answerable fact "
            "identified by route",
            "only then use remaining capacity for general exploration",
        )

        positions = [normalized.index(fragment) for fragment in priority_fragments]
        self.assertEqual(positions, sorted(positions))
        self.assertIn(
            "covering the pr description and any explicitly linked issue or "
            "acceptance material already supplied there",
            normalized,
        )

    def test_standalone_plan_prompt_preserves_priority_order(self):
        self._assert_plan_priority_order(PLAN_PROMPT)

    def test_continuation_plan_prompt_preserves_priority_order(self):
        self._assert_plan_priority_order(PLAN_CONTINUATION_PROMPT)

    def test_provider_contract_ranks_evidence_without_merge_materiality_cues(self):
        provider_contract = "\n".join(
            (
                PLAN_METHOD_PROMPT,
                PLAN_PROMPT,
                PLAN_CONTINUATION_PROMPT,
                RECONCILE_SYSTEM_PROMPT,
            )
        ).casefold()
        normalized_contract = " ".join(provider_contract.split())

        self.assertIn('"unresolved_gaps"', provider_contract)
        self.assertNotIn("material_unknowns", provider_contract)
        self.assertNotIn("affects_merge", provider_contract)
        self.assertNotIn("affects merge readiness", provider_contract)
        self.assertNotIn("change merge judgment", provider_contract)
        self.assertIn("changed callback or event adapter", provider_contract)
        self.assertIn("callback payload shape", provider_contract)
        self.assertIn("returned lifecycle or control handle", provider_contract)
        self.assertIn("do not hand the owner a lookup", provider_contract)
        self.assertIn(
            "first verify concrete author acceptance criteria",
            normalized_contract,
        )
        self.assertIn(
            "second, when the pr changes tests, ci, or validation infrastructure",
            normalized_contract,
        )
        self.assertIn(
            "third verify the highest-consequence locally answerable fact",
            normalized_contract,
        )
        self.assertIn(
            "only then use remaining capacity for general exploration",
            normalized_contract,
        )

    def test_plan_uses_plain_questions_and_author_criteria(self):
        accepted, diagnostics = validate_verification_plan(
            [
                {
                    "question": "Where is Widget constructed?",
                    "why_it_matters": "Callers can expose a compatibility gap.",
                    "tool": "search_code",
                    "args": {"query": "Widget(", "reason": "Find callers."},
                }
            ],
            max_items=6,
        )
        plan = {
            "author_acceptance_criteria": [
                {"criterion": "The migration test must pass before merge."}
            ]
        }
        criteria_diagnostics = _normalize_author_acceptance_criteria(
            plan,
            max_items=8,
        )

        self.assertEqual(diagnostics, [])
        self.assertEqual(len(accepted), 1)
        self.assertNotIn("obligation_id", accepted[0])
        self.assertEqual(criteria_diagnostics, [])
        self.assertEqual(
            plan["author_acceptance_criteria"],
            [{"criterion": "The migration test must pass before merge."}],
        )

    def test_removed_reconcile_roots_are_ignored(self):
        projected, normalizations = _strip_reconcile_extra_fields(
            {
                "summary": "Evidence acquired.",
                "answered": [],
                "unresolved_gaps": [],
                "followups": [],
                "complete": True,
                "decision_obligations": [{"proposition": "ignored"}],
                "obligation_dispositions": [{"status": "satisfied"}],
                "semantic_closure": {"version": 1},
            }
        )

        self.assertEqual(
            set(projected),
            {"summary", "answered", "unresolved_gaps", "followups", "complete"},
        )
        self.assertIn("extra_root_fields_removed:3", normalizations)

    def test_review_context_hides_private_evidence_identities(self):
        state = CollectionState(
            pr_details="details",
            pr_content=_pr_content(),
            repo_full_name="owner/repo",
            head_sha="head123456789",
            default_branch="main",
            runtime=SimpleNamespace(),
            accessible_files={"src/changed.py"},
            repo_inventory=RepoInventory(
                repository="owner/repo",
                requested_sha="head123456789",
                status="complete",
                discoverable_files={"src/changed.py"},
            ),
        )
        question_id = state.evidence_ledger.register_question(
            question="Does the changed file contain Widget?",
            tool="read_file",
            args={"path": "src/changed.py", "reason": "Inspect behavior."},
        )
        event_id = state.evidence_ledger.record_event(
            question_id=question_id,
            tool="read_file",
            args={"path": "src/changed.py", "reason": "Inspect behavior."},
            outcome="hit",
            paths=["src/changed.py"],
            source_ref="pr_head:head123456789",
            coverage_type="full_file",
            observed_state="content_observed",
        )
        state.evidence_ledger.resolve(
            question_id=question_id,
            status="answered",
            evidence_refs=[event_id],
            conclusion="The exact-head file contains Widget.",
        )
        state.tool_events.append(
            {
                "tool": "read_file",
                "outcome": "hit",
                "hit_count": 1,
                "paths": ["src/changed.py"],
                "question_id": question_id,
                "evidence_event_id": event_id,
                "metadata": {"coverage_type": "full_file"},
            }
        )

        rendered = assemble_review_context(state)

        self.assertIn("The exact-head file contains Widget.", rendered)
        self.assertIn(
            "reconcile conclusion: The exact-head file contains Widget.",
            rendered,
        )
        self.assertNotIn("finding:", rendered)
        self.assertIn("coverage=full_file", rendered)
        for marker in ("q_", "ev_", "res_", "obl_", "Semantic Closure"):
            self.assertNotIn(marker, rendered)

    def test_deep_projection_has_no_route_reason_input(self):
        self.assertNotIn(
            "route_reason",
            inspect.signature(_append_pfr_sections).parameters,
        )
        rendered = _append_pfr_sections(
            "## Related Context\n- exact-head evidence",
            repo_facts="- inventory_status: complete",
            owner_docs="",
            plan={},
            reconcile={"summary": "Evidence was acquired."},
            max_chars=4000,
            pfr_reserve=1200,
        )

        self.assertIn("### Evidence Acquisition Coverage", rendered)
        self.assertNotIn("Route context:", rendered)

    def test_deep_context_withholds_default_branch_only_snippet_bodies(self):
        state = CollectionState(
            pr_details="details",
            pr_content=_pr_content(),
            repo_full_name="owner/repo",
            head_sha="head123456789",
            default_branch="main",
            runtime=SimpleNamespace(),
        )
        state.collected_snippets.extend(
            [
                {
                    "path": "src/default_only.py",
                    "start": 1,
                    "end": 1,
                    "kind": "usage",
                    "source": "[source: default branch]",
                    "code": "DEFAULT_BRANCH_BODY_MUST_STAY_PRIVATE",
                    "exact_head_admitted": False,
                },
                {
                    "path": "src/head.py",
                    "start": 1,
                    "end": 1,
                    "kind": "usage",
                    "source": "[source: PR head head1234]",
                    "code": "EXACT_HEAD_BODY_IS_ADMITTED",
                    "exact_head_admitted": True,
                },
            ]
        )
        state.tool_events.append(
            {
                "tool": "search_code",
                "args": {"query": "Widget"},
                "outcome": "hit",
                "hit_count": 1,
                "paths": ["src/default_only.py"],
                "source_ref": "default_branch_search",
                "head_reread_outcome": "default_branch_only",
                "result_summary": "DEFAULT_BRANCH_BODY_MUST_STAY_PRIVATE",
                "metadata": {
                    "coverage_type": "search_snippet",
                    "search_hit_lineage": [
                        {
                            "path": "src/default_only.py",
                            "outcome": "literal_missing_at_head",
                            "head_sha": "head123456789",
                        }
                    ],
                },
            }
        )

        private_context = assemble_context(state)
        reconcile_context, _, reconcile_meta = assemble_reconcile_context(
            state,
            max_chars=180_000,
        )
        deep_context = assemble_review_context(state)

        self.assertIn("DEFAULT_BRANCH_BODY_MUST_STAY_PRIVATE", private_context)
        self.assertTrue(reconcile_meta["complete"])
        self.assertNotIn(
            "DEFAULT_BRANCH_BODY_MUST_STAY_PRIVATE",
            reconcile_context,
        )
        self.assertIn(
            "EXACT_HEAD_BODY_IS_ADMITTED",
            reconcile_context,
        )
        self.assertIn(
            "default-branch hit content withheld",
            reconcile_context,
        )
        state.collected_snippets[0]["code"] = (
            "MUTATED_DEFAULT_BRANCH_BODY_MUST_STAY_PRIVATE"
        )
        state.tool_events[0]["result_summary"] = (
            "MUTATED_DEFAULT_BRANCH_BODY_MUST_STAY_PRIVATE"
        )
        mutated_reconcile_context, _, _ = assemble_reconcile_context(
            state,
            max_chars=180_000,
        )
        mutated_deep_context = assemble_review_context(state)
        self.assertEqual(mutated_reconcile_context, reconcile_context)
        self.assertEqual(mutated_deep_context, deep_context)
        self.assertNotIn("DEFAULT_BRANCH_BODY_MUST_STAY_PRIVATE", deep_context)
        self.assertIn("EXACT_HEAD_BODY_IS_ADMITTED", deep_context)
        self.assertIn("default-branch hit content withheld", deep_context)

    def test_route_contract_has_no_semantic_closure_fields(self):
        route = _validate_route_contract(
            {
                "reviewable_semantic_delta": True,
                "minimum_evidence_boundary": "bounded_repo",
                "reason": "One unchanged caller can change the review judgment.",
                "complexity": "normal",
                "pr_type": "code",
                "risk_domains": ["correctness"],
            }
        )

        self.assertEqual(route["complexity"], "normal")
        self.assertNotIn("primary_review_obligation", route)
        self.assertNotIn("semantic_closure_version", route)


class UniqueSuffixPathCandidateTest(unittest.TestCase):
    """Sanitized weak-path discovery replay and controls."""

    def test_unique_complete_suffix_is_a_weak_candidate_only(self):
        inventory = RepoInventory(
            repository="owner/repo",
            requested_sha="head123456789",
            status="complete",
            discoverable_files={
                "apps/docs/public/example_files/asset.json",
                "src/changed.py",
            },
        )
        candidates = derive_unique_suffix_path_candidates(
            _pr_content()["file_changes"],
            inventory,
        )

        self.assertEqual(len(candidates), 1)
        self.assertEqual(
            candidates[0]["literal_reference"],
            "example_files/asset.json",
        )
        self.assertEqual(
            candidates[0]["candidate_path"],
            "apps/docs/public/example_files/asset.json",
        )
        self.assertIn("weak candidate, not evidence", candidates[0]["basis"])
        self.assertNotIn("evidence_ref", candidates[0])

    def test_ambiguous_partial_and_sensitive_suffixes_fail_closed(self):
        changes = _pr_content()["file_changes"]
        ambiguous = RepoInventory(
            repository="owner/repo",
            requested_sha="head123456789",
            status="complete",
            discoverable_files={
                "apps/a/example_files/asset.json",
                "apps/b/example_files/asset.json",
            },
        )
        partial = RepoInventory(
            repository="owner/repo",
            requested_sha="head123456789",
            status="partial",
            tree_truncated=True,
            discoverable_files={
                "apps/docs/public/example_files/asset.json",
            },
        )
        sensitive = RepoInventory(
            repository="owner/repo",
            requested_sha="head123456789",
            status="complete",
            discoverable_files={"apps/runtime/secrets/api.pem"},
        )

        self.assertEqual(
            derive_unique_suffix_path_candidates(changes, ambiguous), []
        )
        self.assertEqual(
            derive_unique_suffix_path_candidates(changes, partial), []
        )
        self.assertEqual(
            derive_unique_suffix_path_candidates(
                _pr_content(diff='+key = "secrets/api.pem"\n')["file_changes"],
                sensitive,
            ),
            [],
        )

    def test_existing_source_relative_path_is_not_rewritten_as_suffix_hint(self):
        inventory = RepoInventory(
            repository="owner/repo",
            requested_sha="head123456789",
            status="complete",
            discoverable_files={
                "src/example_files/asset.json",
                "src/changed.py",
            },
        )

        self.assertEqual(
            derive_unique_suffix_path_candidates(
                _pr_content()["file_changes"],
                inventory,
            ),
            [],
        )


if __name__ == "__main__":
    unittest.main()
