import hashlib
import json
import unittest
from unittest.mock import patch

from tests.unit.fakes import FakeDynamoResource, FakeS3Client, ensure_repo_root_on_path, install_fake_aws_modules, set_default_env

ensure_repo_root_on_path()
set_default_env()
fake_dynamo = FakeDynamoResource()
install_fake_aws_modules(fake_dynamo)

from lambdas.LlamaPReviewPipeline import persistence


class TestPersistencePipeline(unittest.TestCase):
    def setUp(self):
        self.table = fake_dynamo.Table("llamapreview-pipeline-test")
        self.table.reset()

    def test_persistence_codec_round_trip(self):
        text = "context\n" * 100
        blob = persistence.gzip_b64(text)
        self.assertEqual(persistence.gunzip_b64(blob), text)

    def test_store_context_uses_condition_write(self):
        self.table.put_item(Item={"repo": "owner/repo", "pr_number": 1, "status": "PENDING"})
        ok, attrs = persistence.store_context(
            "owner/repo",
            1,
            context_text="ctx",
            pr_details_text="details",
            meta={"tokens": 1},
            review_mode="normal",
            table=self.table,
        )
        self.assertTrue(ok)
        item = self.table.get_item(Key={"repo": "owner/repo", "pr_number": 1})["Item"]
        self.assertEqual(item["status"], "CONTEXT_READY")
        self.assertEqual(item["review_mode"], "normal")
        self.assertEqual(attrs["context_codec"], "gzip-b64")

    def test_compact_context_meta_names_ci_status_and_aggregate_precisely(self):
        compact = persistence._compact_context_meta(
            {
                "ci_snapshot": {
                    "schema_version": 1,
                    "has_ci": True,
                    "commit_status_state": "success",
                    "aggregate_classification": "failure",
                    "checks": [{"classification": "failure"}],
                    "blocking_checks": [{"classification": "failure"}],
                    "action_required_checks": [],
                    "pending_checks": [],
                    "incomplete_checks": [],
                    "actionable_detail_retrieval": {
                        "outcome": "ok",
                        "attempted_check_count": 2,
                        "enriched_check_count": 2,
                        "unmatched_actionable_check_count": 3,
                        "annotation_count": 2,
                        "annotation_available_count": 14,
                        "annotation_omitted_count": 12,
                        "truncated_check_count": 1,
                        "error_count": 0,
                    },
                    "overall_state": "success",
                }
            }
        )

        ci = compact["ci_snapshot"]
        self.assertEqual(ci["commit_status_state"], "success")
        self.assertEqual(ci["aggregate_classification"], "failure")
        self.assertEqual(ci["blocking_count"], 1)
        self.assertEqual(ci["actionable_detail_outcome"], "ok")
        self.assertEqual(ci["actionable_detail_attempted_count"], 2)
        self.assertEqual(ci["actionable_detail_enriched_count"], 2)
        self.assertEqual(ci["actionable_detail_unmatched_count"], 3)
        self.assertEqual(ci["annotation_count"], 2)
        self.assertEqual(ci["annotation_available_count"], 14)
        self.assertEqual(ci["annotation_omitted_count"], 12)
        self.assertEqual(ci["annotation_truncated_check_count"], 1)
        self.assertEqual(ci["actionable_detail_error_count"], 0)
        self.assertNotIn("overall_state", ci)

    def test_compact_context_meta_keeps_content_free_pfr_terminal_diagnostics(self):
        compact = persistence._compact_context_meta(
            {
                "pfr_terminal_reconcile_round": 2,
                "pfr_terminal_reconcile_trigger": "sweep_hit",
                "pfr_post_terminal_tool_call_count": 0,
                "pfr_sweep_hit_count": 1,
                "terminal_unexecuted_followups": [
                    {
                        "path": "private/source.py",
                        "question": "Inspect private source.",
                    }
                ],
            }
        )

        self.assertEqual(
            compact,
            {
                "pfr_terminal_reconcile_round": 2,
                "pfr_terminal_reconcile_trigger": "sweep_hit",
                "pfr_post_terminal_tool_call_count": 0,
                "pfr_sweep_hit_count": 1,
            },
        )

    def test_compact_context_meta_keeps_v31_content_free_control_plane_health(self):
        compact = persistence._compact_context_meta(
            {
                "pfr_evidence_index_event_count": 25,
                "pfr_evidence_index_complete": True,
                "pfr_evidence_binding_failure_count": 0,
                "pfr_fetch_degradation_reason_counts": {
                    "search_error": 2,
                    "budget_skipped": 1,
                },
                "evidence_event_index": {
                    "events": [{"event_id": "ev_private"}],
                },
            }
        )

        self.assertEqual(compact["pfr_evidence_index_event_count"], 25)
        self.assertTrue(compact["pfr_evidence_index_complete"])
        self.assertEqual(compact["pfr_evidence_binding_failure_count"], 0)
        self.assertEqual(
            compact["pfr_fetch_degradation_reason_counts"],
            {"search_error": 2, "budget_skipped": 1},
        )
        self.assertNotIn("evidence_event_index", compact)

    def test_compact_context_meta_keeps_only_content_free_planning_coverage(self):
        compact = persistence._compact_context_meta(
            {
                "planning_coverage_gap": {
                    "status": "gap",
                    "reason_kinds": [
                        "route_risk_domain_uncovered",
                        "critical_step_budget_skipped",
                    ],
                    "route_risk_domain_count": 2,
                    "covered_route_risk_domain_count": 1,
                    "missing_risk_domains": ["private-domain-name"],
                    "decision_obligation_count": 3,
                    "critical_obligation_count": 1,
                    "repository_obligation_count": 2,
                    "unbound_repository_obligation_indexes": [1],
                    "critical_step_dropped_cap_count": 0,
                    "critical_step_budget_skipped_count": 1,
                    "private_proposition": "Do not persist this model text.",
                }
            }
        )

        coverage = compact["planning_coverage_gap"]
        self.assertEqual(coverage["status"], "gap")
        self.assertEqual(coverage["route_risk_domain_count"], 2)
        self.assertEqual(
            coverage["critical_step_budget_skipped_count"],
            1,
        )
        self.assertNotIn("missing_risk_domains", coverage)
        self.assertNotIn("private_proposition", coverage)
        self.assertNotIn("decision_obligation_count", coverage)
        self.assertNotIn("critical_obligation_count", coverage)
        self.assertNotIn("repository_obligation_count", coverage)
        self.assertNotIn("unbound_repository_obligation_count", coverage)

    def test_idempotency_condition_failure_returns_false(self):
        self.table.put_item(Item={"repo": "owner/repo", "pr_number": 2, "status": "CONTEXT_READY"})
        ok = persistence.update_status(
            "owner/repo",
            2,
            expected_status="PENDING",
            next_status="CONTEXT_READY",
            table=self.table,
        )
        self.assertFalse(ok)
        item = self.table.get_item(Key={"repo": "owner/repo", "pr_number": 2})["Item"]
        self.assertEqual(item["status"], "CONTEXT_READY")

    def test_review_artifact_uses_complete_s3_pointer_when_bucket_is_configured(self):
        self.table.put_item(Item={"repo": "owner/repo", "pr_number": 3, "status": "CONTEXT_READY"})
        fake_s3 = FakeS3Client()
        artifact = {
            "main_comment": "summary",
            "inline_comments": [{"body": "x" * 2000}],
            "fallback_comments": [],
            "head_sha": "abcdef12",
        }
        with patch.object(persistence.config, "CONTEXT_S3_BUCKET", "bucket"), patch.object(
            persistence.config, "MAX_CONTEXT_ITEM_BYTES", 1
        ):
            ok = persistence.store_review_result(
                "owner/repo",
                3,
                expected_status="CONTEXT_READY",
                dry_run=True,
                review_comment="summary",
                artifact=artifact,
                review_mode="high",
                extra_attrs={"route_reason": "test"},
                table=self.table,
                s3_client=fake_s3,
            )
            self.assertTrue(ok)
            item = self.table.get_item(Key={"repo": "owner/repo", "pr_number": 3})["Item"]
            self.assertEqual(item["status"], "PROCESSED_DRYRUN")
            self.assertEqual(item["review_mode"], "high")
            self.assertEqual(item["route_reason"], "test")
            self.assertEqual(item["review_artifact_codec"], "s3-json-gzip-v1")
            self.assertIn("review_artifact_s3_key", item)
            self.assertTrue(item["review_artifact_complete"])
            self.assertNotIn("review_artifact_blob", item)
            loaded = persistence.load_review_artifact_from_item(item, s3_client=fake_s3)
            self.assertEqual(fake_s3.put_calls[0]["ServerSideEncryption"], "AES256")
        self.assertEqual(loaded, artifact)

    def test_context_bundle_uses_pointer_and_restores_full_metadata(self):
        self.table.put_item(
            Item={
                "repo": "owner/repo",
                "pr_number": 4,
                "status": "PENDING",
                "head_sha": "abc123",
                "run_id": "delivery-1",
            }
        )
        fake_s3 = FakeS3Client()
        meta = {"context_strategy": "pfr", "evidence_catalog": [{"id": "ci:build"}], "elapsed_seconds": 1.25}
        with patch.object(persistence.config, "RUN_ARTIFACT_BUCKET", "artifact-bucket"):
            ok, attrs = persistence.store_context(
                "owner/repo",
                4,
                context_text="complete context",
                pr_details_text="complete PR details",
                meta=meta,
                review_mode="high",
                head_sha="abc123",
                run_id="delivery-1",
                table=self.table,
                s3_client=fake_s3,
            )
            self.assertTrue(ok)
            self.assertEqual(attrs["context_codec"], "s3-json-gzip-v1")
            self.assertNotIn("context_s3_key", attrs)
            self.assertNotIn("context_blob", attrs)
            item = self.table.get_item(Key={"repo": "owner/repo", "pr_number": 4})["Item"]
            context, details, loaded_meta = persistence.load_context_bundle_from_item(item, s3_client=fake_s3)
        self.assertEqual(context, "complete context")
        self.assertEqual(details, "complete PR details")
        self.assertEqual(loaded_meta, {**meta, "head_sha": "abc123"})
        self.assertEqual(fake_s3.put_calls[0]["ServerSideEncryption"], "AES256")
        tampered = {**item, "head_sha": "different-head"}
        with self.assertRaises(persistence.ArtifactIntegrityError):
            persistence.load_context_bundle_from_item(tampered, s3_client=fake_s3)

    def test_corrupt_s3_artifact_fails_checksum_validation(self):
        self.table.put_item(Item={"repo": "owner/repo", "pr_number": 5, "status": "CONTEXT_READY"})
        fake_s3 = FakeS3Client()
        artifact = {"main_comment": "summary", "inline_comments": [], "head_sha": "abc"}
        with patch.object(persistence.config, "RUN_ARTIFACT_BUCKET", "artifact-bucket"):
            persistence.store_review_result(
                "owner/repo",
                5,
                expected_status="CONTEXT_READY",
                dry_run=True,
                review_comment="summary",
                artifact=artifact,
                table=self.table,
                s3_client=fake_s3,
            )
            item = self.table.get_item(Key={"repo": "owner/repo", "pr_number": 5})["Item"]
            pointer = item["review_artifact"]
            fake_s3.objects[(pointer["bucket"], pointer["key"])] += b"corrupt"
            with self.assertRaises(persistence.ArtifactIntegrityError):
                persistence.load_review_artifact_from_item(item, s3_client=fake_s3)

    def test_s3_artifact_keys_are_content_addressed_and_replay_stable(self):
        fake_s3 = FakeS3Client()
        with patch.object(persistence.config, "RUN_ARTIFACT_BUCKET", "artifact-bucket"):
            first = persistence._review_artifact_attrs(
                "owner/repo",
                5,
                {"main_comment": "one", "inline_comments": []},
                head_sha="abc",
                run_id="delivery",
                s3_client=fake_s3,
            )
            replay = persistence._review_artifact_attrs(
                "owner/repo",
                5,
                {"main_comment": "one", "inline_comments": []},
                head_sha="abc",
                run_id="delivery",
                s3_client=fake_s3,
            )
            changed = persistence._review_artifact_attrs(
                "owner/repo",
                5,
                {"main_comment": "two", "inline_comments": []},
                head_sha="abc",
                run_id="delivery",
                s3_client=fake_s3,
            )
        self.assertEqual(first["review_artifact_s3_key"], replay["review_artifact_s3_key"])
        self.assertNotEqual(first["review_artifact_s3_key"], changed["review_artifact_s3_key"])
        self.assertIn(first["review_artifact_sha256"][:16], first["review_artifact_s3_key"])

    def test_dry_run_artifact_is_mandatory_even_when_optional_flag_is_off(self):
        self.table.put_item(Item={"repo": "owner/repo", "pr_number": 6, "status": "CONTEXT_READY"})
        with patch.object(persistence.config, "PERSIST_REVIEW_ARTIFACT", False):
            ok = persistence.store_review_result(
                "owner/repo",
                6,
                expected_status="CONTEXT_READY",
                dry_run=True,
                review_comment="summary",
                artifact={"main_comment": "summary", "inline_comments": [], "head_sha": "abc"},
                table=self.table,
            )
        self.assertTrue(ok)
        item = self.table.get_item(Key={"repo": "owner/repo", "pr_number": 6})["Item"]
        self.assertTrue(item["review_artifact_persisted"])
        self.assertTrue(item["review_artifact_complete"])

    def test_typed_review_failure_persists_without_synthetic_public_comment(self):
        self.table.put_item(
            Item={
                "repo": "owner/repo",
                "pr_number": 61,
                "status": "CONTEXT_READY",
            }
        )
        artifact = {
            "main_comment": "",
            "inline_comments": [],
            "review_generation_status": "incomplete",
            "review_publishable": False,
            "review_publication_safe": False,
            "review_failure_kind": "presentation_invalid",
        }

        stored = persistence.store_review_failure(
            "owner/repo",
            61,
            expected_status="CONTEXT_READY",
            artifact=artifact,
            error_kind="presentation_invalid",
            error_stage="review",
            retryable=False,
            retry_exhausted=False,
            attempt=1,
            head_sha="abc",
            run_id="run-61",
            table=self.table,
        )

        self.assertTrue(stored)
        item = self.table.get_item(
            Key={"repo": "owner/repo", "pr_number": 61}
        )["Item"]
        self.assertEqual(item["status"], "ERROR")
        self.assertEqual(
            persistence.load_review_artifact_from_item(item),
            artifact,
        )

    def test_success_result_still_requires_a_public_comment(self):
        self.table.put_item(
            Item={
                "repo": "owner/repo",
                "pr_number": 62,
                "status": "CONTEXT_READY",
            }
        )
        with self.assertRaisesRegex(
            persistence.ArtifactIntegrityError,
            "non-empty main_comment",
        ):
            persistence.store_review_result(
                "owner/repo",
                62,
                expected_status="CONTEXT_READY",
                dry_run=True,
                review_comment="",
                artifact={"main_comment": "", "inline_comments": []},
                table=self.table,
            )

    def test_large_dry_run_artifact_without_bucket_does_not_transition(self):
        self.table.put_item(Item={"repo": "owner/repo", "pr_number": 7, "status": "CONTEXT_READY"})
        with patch.object(persistence.config, "RUN_ARTIFACT_BUCKET", ""), patch.object(
            persistence.config, "CONTEXT_S3_BUCKET", ""
        ), patch.object(persistence.config, "MAX_CONTEXT_ITEM_BYTES", 1):
            with self.assertRaises(persistence.ArtifactIntegrityError):
                persistence.store_review_result(
                    "owner/repo",
                    7,
                    expected_status="CONTEXT_READY",
                    dry_run=True,
                    review_comment="summary",
                    artifact={"main_comment": "summary", "inline_comments": []},
                    table=self.table,
                )
        item = self.table.get_item(Key={"repo": "owner/repo", "pr_number": 7})["Item"]
        self.assertEqual(item["status"], "CONTEXT_READY")

    def test_private_reasoning_cannot_enter_review_artifact(self):
        self.table.put_item(Item={"repo": "owner/repo", "pr_number": 8, "status": "CONTEXT_READY"})
        with self.assertRaises(persistence.ArtifactIntegrityError):
            persistence.store_review_result(
                "owner/repo",
                8,
                expected_status="CONTEXT_READY",
                dry_run=True,
                review_comment="summary",
                artifact={"main_comment": "summary", "reasoning_content": "private"},
                table=self.table,
            )

    def test_phase_attempts_are_counted_independently(self):
        self.table.put_item(Item={"repo": "owner/repo", "pr_number": 9, "status": "PENDING"})
        self.assertEqual(persistence.increment_phase_attempt("owner/repo", 9, "context", table=self.table), 1)
        self.assertEqual(persistence.increment_phase_attempt("owner/repo", 9, "context", table=self.table), 2)
        self.assertEqual(persistence.increment_phase_attempt("owner/repo", 9, "review", table=self.table), 1)
        item = self.table.get_item(Key={"repo": "owner/repo", "pr_number": 9})["Item"]
        self.assertEqual(item["attempt"], 3)
        self.assertEqual(item["context_attempt"], 2)
        self.assertEqual(item["review_attempt"], 1)

    def test_phase_claim_is_atomic_owner_bound_and_expiry_reclaimable(self):
        self.table.put_item(
            Item={
                "repo": "owner/repo",
                "pr_number": 10,
                "status": "PENDING",
            }
        )
        identity = {
            "schema_version": 1,
            "phase": "context",
            "function_version": "42",
            "log_stream_name": "stream-a",
            "aws_request_id": "request-a",
        }
        first = persistence.claim_phase_attempt(
            "owner/repo",
            10,
            "context",
            expected_status="PENDING",
            runtime_identity=identity,
            owner_id="owner-a",
            now_epoch=1,
            table=self.table,
        )
        overlapping = persistence.claim_phase_attempt(
            "owner/repo",
            10,
            "context",
            expected_status="PENDING",
            runtime_identity=identity,
            owner_id="owner-b",
            now_epoch=2,
            table=self.table,
        )
        reclaimed = persistence.claim_phase_attempt(
            "owner/repo",
            10,
            "context",
            expected_status="PENDING",
            runtime_identity=identity,
            owner_id="owner-c",
            now_epoch=2000,
            table=self.table,
        )

        self.assertEqual(first["attempt"], 1)
        self.assertIsNone(overlapping)
        self.assertEqual(reclaimed["attempt"], 2)
        item = self.table.get_item(
            Key={"repo": "owner/repo", "pr_number": 10}
        )["Item"]
        self.assertEqual(item["context_claim"]["owner_id"], "owner-c")
        self.assertEqual(item["context_runtime_identity"], identity)

    def test_phase_transition_requires_owner_and_removes_claim(self):
        self.table.put_item(
            Item={
                "repo": "owner/repo",
                "pr_number": 12,
                "status": "PENDING",
            }
        )
        claim = persistence.claim_phase_attempt(
            "owner/repo",
            12,
            "context",
            expected_status="PENDING",
            runtime_identity={"aws_request_id": "request-a"},
            owner_id="owner-a",
            now_epoch=1,
            table=self.table,
        )
        wrong = {**claim, "owner_id": "owner-b"}
        wrong_owner_stored, _ = persistence.store_context(
            "owner/repo",
            12,
            context_text="ctx",
            pr_details_text="details",
            meta={},
            phase_claim=wrong,
            table=self.table,
        )
        stored, _ = persistence.store_context(
            "owner/repo",
            12,
            context_text="ctx",
            pr_details_text="details",
            meta={},
            phase_claim=claim,
            table=self.table,
        )

        self.assertFalse(wrong_owner_stored)
        self.assertTrue(stored)
        item = self.table.get_item(
            Key={"repo": "owner/repo", "pr_number": 12}
        )["Item"]
        self.assertEqual(item["status"], "CONTEXT_READY")
        self.assertNotIn("context_claim", item)

    def test_provider_calls_are_idempotent_sorted_and_state_scoped(self):
        self.table.put_item(
            Item={
                "repo": "owner/repo",
                "pr_number": 11,
                "status": "PENDING",
            }
        )
        later = {
            "call_id": "b" * 64,
            "pipeline_phase": "context",
            "pipeline_attempt": 1,
            "phase": "pfr_plan",
            "call_index": 1,
            "usage_state": "reported",
            "usage": {
                "total_tokens": 13,
                "prompt_cache_details": {"hit_tokens": 3},
            },
        }
        route = {
            "call_id": "a" * 64,
            "pipeline_phase": "context",
            "pipeline_attempt": 1,
            "phase": "route",
            "call_index": 1,
            "usage_state": "unreported",
            "usage": {},
        }

        self.assertTrue(
            persistence.record_provider_call(
                "owner/repo",
                11,
                expected_status="PENDING",
                record=later,
                table=self.table,
            )
        )
        self.assertTrue(
            persistence.record_provider_call(
                "owner/repo",
                11,
                expected_status="PENDING",
                record=route,
                table=self.table,
            )
        )
        self.assertTrue(
            persistence.record_provider_call(
                "owner/repo",
                11,
                expected_status="PENDING",
                record=route,
                table=self.table,
            )
        )
        item = self.table.get_item(
            Key={"repo": "owner/repo", "pr_number": 11}
        )["Item"]
        records = persistence.provider_call_records(item)
        self.assertEqual(
            [record["call_id"] for record in records],
            ["a" * 64, "b" * 64],
        )
        self.assertEqual(
            int(records[1]["usage"]["prompt_cache_details"]["hit_tokens"]),
            3,
        )
        self.assertFalse(
            persistence.record_provider_call(
                "owner/repo",
                11,
                expected_status="CONTEXT_READY",
                record={
                    **later,
                    "call_id": "c" * 64,
                    "pipeline_phase": "review",
                },
                table=self.table,
            )
        )
        self.assertEqual(len(persistence.provider_call_records(item)), 2)

    def test_provider_dispatch_fence_is_exactly_finalized_by_cas(self):
        self.table.put_item(
            Item={
                "repo": "owner/repo",
                "pr_number": 13,
                "status": "PENDING",
            }
        )
        operation_id = "e" * 64
        call_id = hashlib.sha256(
            json.dumps(
                {
                    "operation_id": operation_id,
                    "transport_attempt_index": 1,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        fence = {
            "schema_version": 2,
            "call_id": call_id,
            "operation_id": operation_id,
            "run_id": "run-13",
            "head_sha": "f" * 40,
            "phase": "route",
            "pipeline_phase": "context",
            "pipeline_attempt": 1,
            "call_index": 1,
            "transport_attempt_index": 1,
            "transport_dispatch_count": 1,
            "transport_attempt_count": 1,
            "model": "deepseek-v4-flash",
            "logical_model": "deepseek-v4-flash",
            "billed_model": "deepseek-v4-flash",
            "thinking": True,
            "reasoning_effort": "high",
            "status": "dispatching",
            "finish_reason": "",
            "elapsed_seconds": 0,
            "last_attempt_elapsed_seconds": 0,
            "usage_state": "unreported",
            "usage": {},
        }
        self.assertTrue(
            persistence.begin_provider_call_dispatch(
                "owner/repo",
                13,
                expected_status="PENDING",
                record=fence,
                table=self.table,
            )
        )
        item = self.table.get_item(
            Key={"repo": "owner/repo", "pr_number": 13}
        )["Item"]
        self.assertEqual(
            persistence.provider_call_records(item)[0]["status"],
            "dispatching",
        )

        terminal = {
            **fence,
            "transport_dispatch_count": 1,
            "status": "completed",
            "finish_reason": "stop",
            "elapsed_seconds": 2.5,
            "last_attempt_elapsed_seconds": 2.5,
            "usage_state": "reported",
            "usage": {
                "prompt_tokens": 3,
                "completion_tokens": 2,
                "total_tokens": 5,
            },
        }
        self.assertTrue(
            persistence.record_provider_call(
                "owner/repo",
                13,
                expected_status="PENDING",
                record=terminal,
                table=self.table,
            )
        )
        finalized = self.table.get_item(
            Key={"repo": "owner/repo", "pr_number": 13}
        )["Item"]
        self.assertEqual(
            persistence.provider_call_records(finalized),
            [terminal],
        )
        self.assertFalse(
            any(
                record.get("status") == "dispatching"
                for record in persistence.provider_call_records(finalized)
            )
        )

    def test_unresolved_provider_dispatch_fence_blocks_every_later_fence(self):
        self.table.put_item(
            Item={
                "repo": "owner/repo",
                "pr_number": 14,
                "status": "PENDING",
            }
        )
        operation_id = "2" * 64
        call_id = hashlib.sha256(
            json.dumps(
                {
                    "operation_id": operation_id,
                    "transport_attempt_index": 1,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        fence = {
            "schema_version": 2,
            "call_id": call_id,
            "operation_id": operation_id,
            "run_id": "run-14",
            "head_sha": "3" * 40,
            "phase": "route",
            "pipeline_phase": "context",
            "pipeline_attempt": 1,
            "call_index": 1,
            "transport_attempt_index": 1,
            "transport_dispatch_count": 1,
            "transport_attempt_count": 1,
            "model": "deepseek-v4-flash",
            "logical_model": "deepseek-v4-flash",
            "billed_model": "deepseek-v4-flash",
            "thinking": True,
            "reasoning_effort": "high",
            "status": "dispatching",
            "finish_reason": "",
            "elapsed_seconds": 0,
            "last_attempt_elapsed_seconds": 0,
            "usage_state": "unreported",
            "usage": {},
        }
        self.assertTrue(
            persistence.begin_provider_call_dispatch(
                "owner/repo",
                14,
                expected_status="PENDING",
                record=fence,
                table=self.table,
            )
        )
        later_operation_id = "5" * 64
        later_call_id = hashlib.sha256(
            json.dumps(
                {
                    "operation_id": later_operation_id,
                    "transport_attempt_index": 1,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        later = {
            **fence,
            "call_id": later_call_id,
            "operation_id": later_operation_id,
            "pipeline_attempt": 2,
        }
        with self.assertRaises(
            persistence.ProviderDispatchFenceUnresolved
        ) as raised:
            persistence.begin_provider_call_dispatch(
                "owner/repo",
                14,
                expected_status="PENDING",
                record=later,
                table=self.table,
            )
        self.assertEqual(raised.exception.record, fence)
        item = self.table.get_item(
            Key={"repo": "owner/repo", "pr_number": 14}
        )["Item"]
        self.assertEqual(len(persistence.provider_call_records(item)), 1)

    def test_provider_call_records_order_modern_phases_before_repair(self):
        phases = [
            "final_presentation_repair",
            "final_presentation",
            "deep_judgment",
            "final_output",
            "deep_thinking",
        ]
        item = {
            "deepseek_all_attempt_model_phases": [
                {
                    "call_id": character * 64,
                    "pipeline_phase": "review",
                    "pipeline_attempt": 1,
                    "phase": phase,
                    "call_index": 1,
                }
                for character, phase in zip("edbca", phases)
            ]
        }

        self.assertEqual(
            [
                record["phase"]
                for record in persistence.provider_call_records(item)
            ],
            [
                "deep_thinking",
                "deep_judgment",
                "final_output",
                "final_presentation",
                "final_presentation_repair",
            ],
        )

    def test_provider_call_id_must_be_stable_sha256_identity(self):
        self.table.put_item(
            Item={
                "repo": "owner/repo",
                "pr_number": 12,
                "status": "PENDING",
            }
        )
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            persistence.record_provider_call(
                "owner/repo",
                12,
                expected_status="PENDING",
                record={"call_id": "not-a-call-id"},
                table=self.table,
            )

    def test_fact_sheet_is_upserted_with_update_item(self):
        persistence.store_repo_fact_sheet(
            "owner/repo",
            "facts",
            head_sha="head-1",
            table=self.table,
        )
        item = self.table.get_item(Key={"repo": "owner/repo", "pr_number": 0})["Item"]
        self.assertEqual(item["fact_sheet"], "facts")
        self.assertEqual(item["fact_sheet_head_sha"], "head-1")
        self.assertEqual(
            item["fact_sheet_schema_version"],
            persistence.REPO_FACT_SHEET_SCHEMA_VERSION,
        )
        self.assertEqual(
            persistence.load_repo_fact_sheet(
                "owner/repo",
                head_sha="head-1",
                table=self.table,
            ),
            "facts",
        )
        self.assertEqual(
            persistence.load_repo_fact_sheet(
                "owner/repo",
                head_sha="different-head",
                table=self.table,
            ),
            "",
        )
        item["fact_sheet_schema_version"] = "stale-builder-version"
        self.assertEqual(
            persistence.load_repo_fact_sheet(
                "owner/repo",
                head_sha="head-1",
                table=self.table,
            ),
            "",
        )
        item.pop("fact_sheet_schema_version")
        self.assertEqual(
            persistence.load_repo_fact_sheet(
                "owner/repo",
                head_sha="head-1",
                table=self.table,
            ),
            "",
        )
        self.assertEqual(len(self.table.put_calls), 0)

    def test_item_size_guard_runs_before_dynamodb_update(self):
        self.table.put_item(Item={"repo": "owner/repo", "pr_number": 10, "status": "PENDING"})
        with patch.object(persistence.config, "MAX_DYNAMODB_WIRE_BYTES", 20):
            with self.assertRaises(persistence.DynamoItemTooLarge):
                persistence.update_status(
                    "owner/repo",
                    10,
                    expected_status="PENDING",
                    next_status="CONTEXT_READY",
                    attributes={"context_meta": {"large": "x" * 100}},
                    table=self.table,
                )
        self.assertEqual(self.table.get_item(Key={"repo": "owner/repo", "pr_number": 10})["Item"]["status"], "PENDING")


if __name__ == "__main__":
    unittest.main()
