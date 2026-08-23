import copy
import unittest
from unittest.mock import patch

from tests.unit.fakes import (
    FakeDynamoResource,
    ensure_repo_root_on_path,
    install_fake_aws_modules,
    install_fake_jwt_module,
    install_fake_requests_module,
    set_default_env,
)

ensure_repo_root_on_path()
set_default_env()
install_fake_aws_modules(FakeDynamoResource())
install_fake_jwt_module()
install_fake_requests_module()

from lambdas.LlamaPReviewPipeline import pipeline_publication
from lambdas.LlamaPReviewPipeline.errors import (
    HeadVerificationUnavailable,
    PRLifecycleSuperseded,
    PublicationIntegrityFailure,
    PublicationStateConflict,
)
from lambdas.LlamaPReviewPipeline.review import publication_candidate
from lambdas.LlamaPReviewPipeline.review.publish import (
    PUBLIC_FOOTER_MARKER,
    public_footer,
    PreparedGitHubReview,
    build_diff_maps_from_pr_files,
    prepare_review_publication,
)


HEAD = "a" * 40


class _Runtime:
    def __init__(
        self,
        *,
        state="open",
        merged=False,
        head=HEAD,
        locked=False,
    ):
        self.snapshot = {
            "head_sha": head,
            "state": state,
            "merged": merged,
            "locked": locked,
        }

    def get_pr_head_snapshot(self, repo, pr_number):
        return dict(self.snapshot)


class _HeadOnlyRuntime:
    def get_pr_head_sha(self, repo, pr_number):
        return HEAD


def _context(*, kind="ordinary_review", disposition="open_same_head"):
    return pipeline_publication.PublicationContext(
        repo="owner/repo",
        pr_number=7,
        head_sha=HEAD,
        expected_status="PENDING",
        phase="context",
        run_id="run-7",
        generation_attempt=1,
        runtime_identity={"source_commit": "b" * 40},
        phase_claim={
            "phase": "context",
            "attempt": 1,
            "owner_id": "request-1",
            "stream_event_id": "event-1",
        },
        dry_run=False,
        publication_kind=kind,
        required_disposition=disposition,
    )


def _publishable_review():
    review = {
        "visible_verdict": "blocked_findings",
        "decision": {
            "public_sentence": "Don't merge yet — repair the unsafe branch.",
            "reasons": [{"text": "The unsafe branch blocks merge.", "refs": ["F1"]}],
        },
        "owner_action": [{"text": "Repair the unsafe branch and rerun its test."}],
        "findings": [
            {
                "id": "F1",
                "priority": "P1",
                "blocking": True,
                "visibility": "inline",
                "headline": "The branch returns the unsafe value",
                "file_path": "src/app.py",
                "code_snippet": "return unsafe",
                "comment": "Return the validated value instead.",
                "confidence": "High",
                "evidence_status": "verified",
                "claim_scope": "changed_region",
                "evidence_refs": [],
            }
        ],
        "material_unknowns": [
            {
                "claim": "The downstream migration has not been observed.",
                "how_to_check": "Run the migration fixture.",
                "affects_merge": True,
            }
        ],
        "evidence_scope": [
            {"description": "Read the changed region in `src/app.py`."}
        ],
        "diagram": {
            "purpose": "risk_path",
            "description": "The retained failure path.",
            "mermaid": "```mermaid\nsequenceDiagram\nA->>B: unsafe\n```",
        },
        "rendering_plan": {
            "ci_public_state": {
                "posture": "unresolved",
                "retrieval_outcome": "partial",
                "counts": {"failure": 1, "success": 2},
            }
        },
    }
    return {
        "review_generation_status": "complete",
        "review_publishable": True,
        "review_publication_safe": True,
        "review_fallback_used": False,
        "pr_review_comment": "### LlamaPReview — Blocking issues found",
        "inline_comments": [
            {
                "file_path": "src/app.py",
                "code_snippet": "return unsafe",
                "comment": "Return the validated value instead.",
                "priority": "P1",
                "confidence": "High",
            }
        ],
        "v3_review": review,
        "review_quality_warnings": [],
    }


class LifecyclePublicationPrimitiveTests(unittest.TestCase):
    def test_prepared_kind_requires_its_exact_disposition(self):
        with self.assertRaisesRegex(ValueError, "do not agree"):
            PreparedGitHubReview(
                head_sha=HEAD,
                main_body="body",
                comments=(),
                artifact={},
                publication_kind="ordinary_review",
                required_disposition="merged_same_head",
            )

    def test_post_merge_projection_is_structured_and_has_no_inline_payload(self):
        diff_maps = build_diff_maps_from_pr_files(
            [
                {
                    "filename": "src/app.py",
                    "patch": "@@ -1,1 +1,1 @@\n-return old\n+return unsafe",
                }
            ]
        )
        prepared = prepare_review_publication(
            _publishable_review(),
            head_sha=HEAD,
            diff_maps=diff_maps,
            publication_kind="post_merge_follow_up",
        )

        self.assertEqual(prepared.publication_kind, "post_merge_follow_up")
        self.assertEqual(prepared.required_disposition, "merged_same_head")
        self.assertEqual(prepared.comments, ())
        self.assertEqual(prepared.artifact["inline_comments"], [])
        self.assertIn("### LlamaPReview — Post-merge follow-up", prepared.main_body)
        self.assertNotIn("Don't merge", prepared.main_body)
        self.assertNotIn("blocks merge", prepared.main_body)
        self.assertIn("Repair the unsafe branch", prepared.main_body)
        self.assertIn("The branch returns the unsafe value", prepared.main_body)
        self.assertIn("The downstream migration", prepared.main_body)
        self.assertIn("Exact-head CI remains unresolved", prepared.main_body)
        self.assertIn("```mermaid", prepared.main_body)
        self.assertIn("### Follow-up locations", prepared.main_body)
        self.assertIn(
            "`src/app.py:1` — Return the validated value instead.",
            prepared.main_body,
        )
        self.assertEqual(prepared.main_body.count(PUBLIC_FOOTER_MARKER), 1)

    def test_ordinary_projection_remains_the_existing_payload_shape(self):
        final = _publishable_review()
        final["inline_comments"] = []
        prepared = prepare_review_publication(
            final,
            head_sha=HEAD,
            diff_maps={},
        )

        self.assertEqual(prepared.publication_kind, "ordinary_review")
        self.assertEqual(prepared.required_disposition, "open_same_head")
        self.assertEqual(
            prepared.main_body,
            final["pr_review_comment"] + public_footer(HEAD),
        )

    def test_post_merge_locations_keep_final_inline_order(self):
        final = _publishable_review()
        final["inline_comments"] = [
            {
                "file_path": "z.py",
                "code_snippet": "unsafe_z()",
                "comment": "Repair Z first.",
            },
            {
                "file_path": "a.py",
                "code_snippet": "unsafe_a()",
                "comment": "Repair A second.",
            },
        ]
        diff_maps = build_diff_maps_from_pr_files(
            [
                {
                    "filename": "a.py",
                    "patch": "@@ -1 +1 @@\n-old_a()\n+unsafe_a()",
                },
                {
                    "filename": "z.py",
                    "patch": "@@ -1 +1 @@\n-old_z()\n+unsafe_z()",
                },
            ]
        )

        prepared = prepare_review_publication(
            final,
            head_sha=HEAD,
            diff_maps=diff_maps,
            publication_kind="post_merge_follow_up",
        )

        self.assertLess(
            prepared.main_body.index("`z.py:1` — Repair Z first."),
            prepared.main_body.index("`a.py:1` — Repair A second."),
        )

    def test_post_merge_ci_copy_is_factual_without_open_pr_gating_language(self):
        states = (
            {
                "posture": "resolved",
                "retrieval_outcome": "ok",
                "counts": {"success": 2},
            },
            {
                "posture": "unresolved",
                "retrieval_outcome": "partial",
                "counts": {"failure": 1, "pending": 2},
            },
            {
                "posture": "unrelated_supported",
                "retrieval_outcome": "ok",
                "counts": {"failure": 1},
            },
            {
                "posture": "not_observed",
                "retrieval_outcome": "no_hit",
                "counts": {},
            },
            {
                "posture": "unresolved",
                "retrieval_outcome": "error",
                "counts": {"action_required": 1, "incomplete": 1},
            },
        )
        banned = (
            "merge-safety",
            "don't merge yet",
            "blocks merge",
            "before merging",
            "merge readiness",
        )
        for state in states:
            with self.subTest(state=state):
                final = _publishable_review()
                final["inline_comments"] = []
                final["v3_review"]["rendering_plan"]["ci_public_state"] = state
                prepared = prepare_review_publication(
                    final,
                    head_sha=HEAD,
                    diff_maps={},
                    publication_kind="post_merge_follow_up",
                )
                visible = prepared.main_body.casefold()
                for phrase in banned:
                    self.assertNotIn(phrase, visible)
                self.assertEqual(prepared.comments, ())
                self.assertEqual(prepared.main_body.count(PUBLIC_FOOTER_MARKER), 1)
                if state["retrieval_outcome"] in {"partial", "error"}:
                    self.assertIn(state["retrieval_outcome"], visible)

    def test_cancellation_is_exact_unscoreable_and_uses_same_commit_boundary(self):
        captured = {}

        def capture(prepared, terminal_attributes, **kwargs):
            captured["prepared"] = prepared
            captured["terminal"] = terminal_attributes
            return True

        context = _context(
            kind="lifecycle_cancellation",
            disposition="merged_same_head",
        )
        with patch.object(
            pipeline_publication,
            "_require_publication_disposition",
        ), patch.object(
            pipeline_publication,
            "commit_prepared",
            side_effect=capture,
        ):
            result = pipeline_publication.commit_lifecycle_cancellation(
                context=context,
                runtime=_Runtime(state="closed", merged=True),
                lifecycle="merged",
            )

        self.assertTrue(result)
        prepared = captured["prepared"]
        self.assertEqual(
            prepared.main_body,
            pipeline_publication.MERGED_CANCELLATION_BODY,
        )
        self.assertEqual(prepared.comments, ())
        self.assertNotIn(PUBLIC_FOOTER_MARKER, prepared.main_body)
        self.assertNotIn("mermaid", prepared.main_body.casefold())
        self.assertEqual(
            captured["terminal"]["review_generation_status"],
            "cancelled",
        )
        self.assertFalse(captured["terminal"]["quality_scoreable"])
        self.assertEqual(
            captured["terminal"]["quality_exclusion_reasons"],
            ["pr_merged_before_review_complete"],
        )

    def test_pre_publish_requires_candidate_disposition_not_generic_ended(self):
        item = {
            "status": "PENDING",
            "initial_admission": {
                "schema_version": 1,
                "disposition": "open_same_head",
                "head_sha": HEAD,
                "run_id": "run-7",
                "admitted_at": "2026-08-16T00:00:00+00:00",
            },
        }
        with patch.object(
            pipeline_publication.persistence,
            "get_item",
            return_value=item,
        ), patch.object(
            pipeline_publication,
            "fetch_pr_details",
            return_value=({}, []),
        ):
            ordinary = pipeline_publication.make_pre_publish_check(
                _context(),
                _Runtime(state="closed", merged=True),
                check_duplicate=False,
            )
            with self.assertRaises(PRLifecycleSuperseded):
                ordinary()

            merged = pipeline_publication.make_pre_publish_check(
                _context(
                    kind="post_merge_follow_up",
                    disposition="merged_same_head",
                ),
                _Runtime(state="closed", merged=True),
                check_duplicate=False,
            )
            merged()

            without_admission = pipeline_publication.make_pre_publish_check(
                _context(
                    kind="post_merge_follow_up",
                    disposition="merged_same_head",
                ),
                _Runtime(state="closed", merged=True),
                check_duplicate=False,
            )
            item.pop("initial_admission")
            with self.assertRaises(PublicationStateConflict):
                without_admission()

    def test_pre_publish_unverified_and_ended_new_head_fail_with_typed_retry_paths(self):
        context = _context(
            kind="post_merge_follow_up",
            disposition="merged_same_head",
        )
        with self.assertRaises(HeadVerificationUnavailable):
            pipeline_publication._require_publication_disposition(
                context,
                _HeadOnlyRuntime(),
                stage="publication.pre_dispatch",
            )
        with self.assertRaises(PRLifecycleSuperseded) as raised:
            pipeline_publication._require_publication_disposition(
                context,
                _Runtime(state="closed", merged=True, head="b" * 40),
                stage="publication.pre_dispatch",
            )
        self.assertEqual(raised.exception.actual_head_sha, "b" * 40)

    def test_locked_ended_pre_dispatch_aborts_before_full_pr_fetch(self):
        context = _context(
            kind="lifecycle_cancellation",
            disposition="merged_same_head",
        )
        item = {
            "status": "PENDING",
            "initial_admission": {
                "schema_version": 1,
                "disposition": "open_same_head",
                "head_sha": HEAD,
                "run_id": "run-7",
                "admitted_at": "2026-08-16T00:00:00+00:00",
            },
        }
        with patch.object(
            pipeline_publication.persistence,
            "get_item",
            return_value=item,
        ), patch.object(
            pipeline_publication,
            "fetch_pr_details",
        ) as fetch:
            check = pipeline_publication.make_pre_publish_check(
                context,
                _Runtime(state="closed", merged=True, locked=True),
                check_duplicate=False,
            )
            with self.assertRaises(PRLifecycleSuperseded) as raised:
                check()

        self.assertEqual(
            raised.exception.superseded_kind,
            "publication_unavailable_locked",
        )
        fetch.assert_not_called()

    def test_failed_ordinary_review_rechecks_open_lock_before_dispatch(self):
        context = _context()
        item = {"status": "PENDING"}
        for locked, expected in (
            (True, PRLifecycleSuperseded),
            (None, HeadVerificationUnavailable),
        ):
            with self.subTest(locked=locked), patch.object(
                pipeline_publication.persistence,
                "get_item",
                return_value=item,
            ), patch.object(
                pipeline_publication,
                "fetch_pr_details",
            ) as fetch:
                check = pipeline_publication.make_pre_publish_check(
                    context,
                    _Runtime(locked=locked),
                    check_duplicate=False,
                    require_unlocked=True,
                )
                with self.assertRaises(expected) as raised:
                    check()

            if locked is True:
                self.assertEqual(
                    raised.exception.superseded_kind,
                    "publication_unavailable_locked",
                )
            fetch.assert_not_called()

    def test_locked_ended_transition_precedes_lifecycle_mismatch(self):
        context = _context(
            kind="lifecycle_cancellation",
            disposition="closed_same_head",
        )
        with self.assertRaises(PRLifecycleSuperseded) as locked:
            pipeline_publication._require_publication_disposition(
                context,
                _Runtime(state="closed", merged=True, locked=True),
                stage="publication.pre_dispatch",
            )
        self.assertEqual(
            locked.exception.superseded_kind,
            "publication_unavailable_locked",
        )

        with self.assertRaises(PublicationStateConflict):
            pipeline_publication._require_publication_disposition(
                context,
                _Runtime(state="closed", merged=True, locked=False),
                stage="publication.pre_dispatch",
            )

    def test_locked_prepared_intent_records_aborted_before_dispatch(self):
        intent = {
            "state": "prepared",
            "publication_key": "1" * 32,
            "publication_kind": "post_merge_follow_up",
            "required_disposition": "merged_same_head",
        }
        error = PRLifecycleSuperseded(
            HEAD,
            HEAD,
            current_state="closed",
            merged=True,
            stage="publication.pre_publish_disposition",
            superseded_kind="publication_unavailable_locked",
        )
        candidate_accounting = {
            "deepseek_model_phases": [{"phase": "route"}],
            "deepseek_all_attempt_model_phases": [{"phase": "route"}],
            "deepseek_usage_total": {"total_tokens": 17},
            "deepseek_usage_accounting": {
                "complete_numeric_usage": True,
                "unreported_usage_call_count": 0,
            },
        }
        with patch.object(
            pipeline_publication.persistence,
            "get_item",
            return_value={"publication_intent": intent},
        ), patch.object(
            pipeline_publication,
            "load_candidate",
            return_value={"terminal_attributes": candidate_accounting},
        ):
            attrs = pipeline_publication.failure_attributes(
                repo="owner/repo",
                pr_number=7,
                exc=error,
                base={
                    "deepseek_usage_accounting": {
                        "complete_numeric_usage": True
                    }
                },
            )

        self.assertEqual(attrs["publication_status"], "aborted_before_dispatch")
        self.assertTrue(attrs["publication_unavailable_locked"])
        self.assertFalse(attrs["quality_scoreable"])
        self.assertEqual(
            attrs["quality_exclusion_reasons"],
            ["publication_unavailable_locked"],
        )
        self.assertTrue(
            attrs["deepseek_usage_accounting"]["complete_numeric_usage"]
        )
        self.assertEqual(attrs["deepseek_usage_total"]["total_tokens"], 17)
        self.assertEqual(
            attrs["deepseek_all_attempt_model_phases"],
            [{"phase": "route"}],
        )

    def test_candidate_and_intent_bind_kind_and_disposition(self):
        prepared = PreparedGitHubReview(
            head_sha=HEAD,
            main_body="body",
            comments=(),
            artifact={"main_comment": "body"},
            publication_kind="lifecycle_cancellation",
            required_disposition="closed_same_head",
        )
        candidate = publication_candidate.build_candidate(
            prepared,
            repo="owner/repo",
            pr_number=7,
            run_id="run-7",
            phase="context",
            owner_event_id="event-1",
            owner_request_id="request-1",
            publication_generation_attempt=1,
            preflight_completed_at="2026-08-16T00:00:00+00:00",
            generation_runtime_identity={},
            terminal_attributes={},
            publication_key="1" * 32,
        )
        claim = {
            "phase": "context",
            "attempt": 1,
            "owner_id": "request-1",
            "stream_event_id": "event-1",
        }
        pointer = {"sha256": "2" * 64, "key": "candidate.json"}
        with patch.object(
            publication_candidate.persistence,
            "store_publication_candidate",
            return_value=pointer,
        ), patch.object(
            publication_candidate.persistence,
            "store_publication_intent",
            return_value=True,
        ) as store_intent:
            intent = publication_candidate.persist_prepared_intent(
                candidate,
                expected_status="PENDING",
                phase_claim=claim,
            )

        self.assertEqual(intent["publication_kind"], "lifecycle_cancellation")
        self.assertEqual(intent["required_disposition"], "closed_same_head")
        self.assertEqual(
            store_intent.call_args.kwargs["intent"]["publication_kind"],
            "lifecycle_cancellation",
        )

        unsafe = copy.deepcopy(candidate)
        unsafe.pop("required_disposition")
        with patch.object(
            publication_candidate.persistence,
            "load_publication_candidate",
            return_value=unsafe,
        ):
            with self.assertRaises(PublicationIntegrityFailure):
                publication_candidate.load_candidate(intent)

    def test_v1_candidate_gets_only_the_historical_ordinary_binding(self):
        candidate = publication_candidate.build_candidate(
            PreparedGitHubReview(
                head_sha=HEAD,
                main_body="body",
                comments=(),
                artifact={"main_comment": "body"},
            ),
            repo="owner/repo",
            pr_number=7,
            run_id="run-7",
            phase="review",
            owner_event_id="event-1",
            owner_request_id="request-1",
            publication_generation_attempt=1,
            preflight_completed_at="2026-08-16T00:00:00+00:00",
            generation_runtime_identity={},
            terminal_attributes={},
            publication_key="1" * 32,
        )
        intent = {
            "schema_version": 1,
            "publication_key": candidate["publication_key"],
        }
        candidate["publication_schema_version"] = 1
        candidate.pop("publication_kind")
        candidate.pop("required_disposition")
        candidate["terminal_attributes"].pop("publication_kind", None)
        candidate["terminal_attributes"].pop("required_disposition", None)
        with patch.object(
            publication_candidate.persistence,
            "load_publication_candidate",
            return_value=candidate,
        ):
            loaded = publication_candidate.load_candidate(intent)
        self.assertEqual(loaded["publication_kind"], "ordinary_review")
        self.assertEqual(loaded["required_disposition"], "open_same_head")

        v2_missing = copy.deepcopy(candidate)
        v2_missing["publication_schema_version"] = 2
        with patch.object(
            publication_candidate.persistence,
            "load_publication_candidate",
            return_value=v2_missing,
        ):
            with self.assertRaises(PublicationIntegrityFailure):
                publication_candidate.load_candidate({"schema_version": 2})

    def test_prepared_intent_is_retained_when_lifecycle_changes_before_dispatch(self):
        intent = {
            "state": "prepared",
            "publication_key": "1" * 32,
            "publication_kind": "ordinary_review",
            "required_disposition": "open_same_head",
        }
        error = PRLifecycleSuperseded(
            HEAD,
            HEAD,
            current_state="closed",
            merged=True,
            stage="publication.pre_dispatch",
        )

        with patch.object(
            pipeline_publication.persistence,
            "get_item",
            return_value={"publication_intent": intent},
        ):
            attrs = pipeline_publication.failure_attributes(
                repo="owner/repo",
                pr_number=7,
                exc=error,
                base={"deepseek_usage_accounting": {"complete_numeric_usage": True}},
            )

        self.assertEqual(attrs["publication_status"], "aborted_before_dispatch")
        self.assertEqual(attrs["publication_key"], "1" * 32)
        self.assertEqual(attrs["publication_kind"], "ordinary_review")
        self.assertEqual(attrs["required_disposition"], "open_same_head")
        self.assertTrue(
            attrs["deepseek_usage_accounting"]["complete_numeric_usage"]
        )


if __name__ == "__main__":
    unittest.main()
