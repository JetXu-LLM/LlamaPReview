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
fake_dynamo = FakeDynamoResource()
install_fake_aws_modules(fake_dynamo)
install_fake_jwt_module()
install_fake_requests_module()

from lambdas.LlamaPReviewPipeline import (
    lambda_function,
    persistence,
    pipeline_admission,
)
from lambdas.LlamaPReviewPipeline.errors import PRLifecycleSuperseded


REPO = "owner/repo"
PR = 17
HEAD_A = "a" * 40
HEAD_B = "b" * 40
CALL_ID = "c" * 64


class SnapshotRuntime:
    def __init__(self, snapshot):
        self.snapshot = snapshot

    def get_pr_head_snapshot(self, _repo, _pr_number):
        return dict(self.snapshot)


def _claim(*, attempt=1):
    return {
        "schema_version": 1,
        "phase": "context",
        "owner_id": "request-1",
        "stream_event_id": "stream-a",
        "attempt": attempt,
        "expires_at_epoch": 9999999999,
    }


def _encode(value):
    if isinstance(value, bool):
        return {"BOOL": value}
    if isinstance(value, int):
        return {"N": str(value)}
    if isinstance(value, dict):
        return {"M": {key: _encode(child) for key, child in value.items()}}
    if isinstance(value, list):
        return {"L": [_encode(child) for child in value]}
    return {"S": str(value)}


def _stream_record(old, new):
    return {
        "eventID": "successor-stream-event",
        "eventName": "MODIFY",
        "dynamodb": {
            "OldImage": {key: _encode(value) for key, value in old.items()},
            "NewImage": {key: _encode(value) for key, value in new.items()},
        },
    }


class LifecycleDispositionTests(unittest.TestCase):
    def test_full_lifecycle_matrix_and_unverified_snapshot(self):
        cases = (
            ("open", False, HEAD_A, "open_same_head"),
            ("open", False, HEAD_B, "open_new_head"),
            ("closed", True, HEAD_A, "merged_same_head"),
            ("closed", True, HEAD_B, "merged_new_head"),
            ("closed", False, HEAD_A, "closed_same_head"),
            ("closed", False, HEAD_B, "closed_new_head"),
        )
        for state, merged, actual, expected in cases:
            with self.subTest(expected=expected):
                disposition = pipeline_admission.current_pr_disposition(
                    SnapshotRuntime(
                        {
                            "head_sha": actual,
                            "state": state,
                            "merged": merged,
                            "locked": False,
                        }
                    ),
                    REPO,
                    PR,
                    HEAD_A,
                    stage="context.ingest",
                )
                self.assertEqual(disposition.kind.value, expected)

        unverified = pipeline_admission.current_pr_disposition(
            SnapshotRuntime({"head_sha": HEAD_A, "state": "open"}),
            REPO,
            PR,
            HEAD_A,
            stage="context.ingest",
        )
        self.assertEqual(unverified.kind.value, "unverified")

        ended_without_lock = pipeline_admission.current_pr_disposition(
            SnapshotRuntime(
                {"head_sha": HEAD_A, "state": "closed", "merged": True}
            ),
            REPO,
            PR,
            HEAD_A,
            stage="context.ingest",
        )
        self.assertEqual(ended_without_lock.kind.value, "unverified")

        malformed_lock = pipeline_admission.current_pr_disposition(
            SnapshotRuntime(
                {
                    "head_sha": HEAD_A,
                    "state": "closed",
                    "merged": True,
                    "locked": "true",
                }
            ),
            REPO,
            PR,
            HEAD_A,
            stage="context.ingest",
        )
        self.assertEqual(malformed_lock.kind.value, "unverified")

    def test_disposition_carries_structural_lock_state(self):
        disposition = pipeline_admission.current_pr_disposition(
            SnapshotRuntime(
                {
                    "head_sha": HEAD_A,
                    "state": "closed",
                    "merged": True,
                    "locked": True,
                }
            ),
            REPO,
            PR,
            HEAD_A,
            stage="context.ingest",
        )

        self.assertEqual(disposition.kind.value, "merged_same_head")
        self.assertTrue(disposition.locked)

    def test_assert_current_head_classifies_ended_before_changed_head(self):
        runtime = SnapshotRuntime(
            {
                "head_sha": HEAD_B,
                "state": "closed",
                "merged": True,
                "locked": False,
            }
        )
        with self.assertRaises(PRLifecycleSuperseded) as raised:
            pipeline_admission.assert_current_head(
                runtime,
                REPO,
                PR,
                HEAD_A,
                stage="context.ingest",
            )
        self.assertEqual(raised.exception.superseded_kind, "pr_merged")
        self.assertEqual(raised.exception.actual_head_sha, HEAD_B)


class AdmissionPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.table = fake_dynamo.Table("llamapreview-pipeline-test")
        self.table.reset()

    def _put_pending(self, **overrides):
        item = {
            "repo": REPO,
            "pr_number": PR,
            "status": "PENDING",
            "installation_id": 123,
            "head_sha": HEAD_A,
            "head_ref": "feature",
            "base_ref": "main",
            "default_branch": "main",
            "pr_title": "Change",
            "delivery_id": "delivery-a",
            "run_id": "run-a",
            "created_at": "2026-08-16T00:00:00+00:00",
            "updated_at": "2026-08-16T00:00:00+00:00",
            "ttl_epoch": 9999999999,
            "attempt": 1,
            "context_attempt": 1,
            "review_attempt": 0,
            "context_claim": _claim(),
        }
        item.update(overrides)
        self.table.put_item(Item=item)
        return item

    def test_initial_admission_is_owner_bound_and_idempotent(self):
        self._put_pending()
        self.assertTrue(
            persistence.record_initial_admission(
                REPO,
                PR,
                expected_status="PENDING",
                expected_head_sha=HEAD_A,
                phase_claim=_claim(),
                table=self.table,
            )
        )
        first = persistence.get_item(REPO, PR, table=self.table)[
            "initial_admission"
        ]
        self.assertEqual(first["disposition"], "open_same_head")
        self.assertEqual(first["run_id"], "run-a")
        self.assertTrue(
            persistence.record_initial_admission(
                REPO,
                PR,
                expected_status="PENDING",
                expected_head_sha=HEAD_A,
                phase_claim=_claim(),
                table=self.table,
            )
        )
        second = persistence.get_item(REPO, PR, table=self.table)[
            "initial_admission"
        ]
        self.assertEqual(second, first)

    def test_successor_replaces_stale_work_and_preserves_attempt_ledger(self):
        provider_record = {
            "call_id": CALL_ID,
            "status": "completed",
            "pipeline_phase": "context",
            "pipeline_attempt": 1,
            "run_id": "run-a",
            "head_sha": HEAD_A,
        }
        old = self._put_pending(
            initial_admission={
                "schema_version": 1,
                "disposition": "open_same_head",
                "head_sha": HEAD_A,
                "run_id": "run-a",
                "admitted_at": "2026-08-16T00:00:01+00:00",
            },
            context_blob="stale",
            review_mode="high",
            publication_status="not_published",
            error_kind="stale",
            **{f"{persistence.PROVIDER_CALL_ATTR_PREFIX}{CALL_ID}": provider_record},
        )
        self.assertTrue(
            persistence.requeue_head_successor(
                REPO,
                PR,
                expected_status="PENDING",
                expected_head_sha=HEAD_A,
                actual_head_sha=HEAD_B,
                stage="context.pre_reconcile",
                phase_claim=_claim(),
                table=self.table,
            )
        )
        new = persistence.get_item(REPO, PR, table=self.table)
        self.assertEqual(new["status"], "PENDING")
        self.assertEqual(new["head_sha"], HEAD_B)
        self.assertEqual(
            new["run_id"], persistence.head_successor_run_id(REPO, PR, HEAD_B)
        )
        self.assertEqual(new["head_successor_count"], 1)
        self.assertEqual(new["context_attempt"], 1)
        self.assertEqual(new["review_attempt"], 0)
        self.assertEqual(new[f"{persistence.PROVIDER_CALL_ATTR_PREFIX}{CALL_ID}"], provider_record)
        self.assertEqual(
            new["head_predecessor_receipt"]["provider_call_ids"], [CALL_ID]
        )
        for stale in (
            "initial_admission",
            "context_claim",
            "context_blob",
            "review_mode",
            "publication_status",
            "error_kind",
        ):
            self.assertNotIn(stale, new)
        self.assertTrue(persistence.is_valid_head_successor_transition(old, new))
        self.assertFalse(
            persistence.requeue_head_successor(
                REPO,
                PR,
                expected_status="PENDING",
                expected_head_sha=HEAD_B,
                actual_head_sha="d" * 40,
                stage="context.ingest",
                phase_claim=_claim(),
                table=self.table,
            )
        )

    def test_successor_rejects_unresolved_fence_and_exhausted_attempt(self):
        self._put_pending(
            **{
                f"{persistence.PROVIDER_CALL_ATTR_PREFIX}{CALL_ID}": {
                    "call_id": CALL_ID,
                    "status": "dispatching",
                }
            }
        )
        self.assertFalse(
            persistence.requeue_head_successor(
                REPO,
                PR,
                expected_status="PENDING",
                expected_head_sha=HEAD_A,
                actual_head_sha=HEAD_B,
                stage="context.pre_reconcile",
                phase_claim=_claim(),
                table=self.table,
            )
        )
        self._put_pending(
            context_attempt=int(persistence.config.MAX_ATTEMPTS),
            context_claim=_claim(attempt=int(persistence.config.MAX_ATTEMPTS)),
        )
        self.assertFalse(
            persistence.requeue_head_successor(
                REPO,
                PR,
                expected_status="PENDING",
                expected_head_sha=HEAD_A,
                actual_head_sha=HEAD_B,
                stage="context.ingest",
                phase_claim=_claim(attempt=int(persistence.config.MAX_ATTEMPTS)),
                table=self.table,
            )
        )

    def test_successor_stream_requires_exact_attempts_and_complete_ledger(self):
        provider_record = {
            "call_id": CALL_ID,
            "status": "completed",
            "pipeline_phase": "context",
            "pipeline_attempt": 1,
            "run_id": "run-a",
            "head_sha": HEAD_A,
        }
        old = self._put_pending(
            **{f"{persistence.PROVIDER_CALL_ATTR_PREFIX}{CALL_ID}": provider_record}
        )
        self.assertTrue(
            persistence.requeue_head_successor(
                REPO,
                PR,
                expected_status="PENDING",
                expected_head_sha=HEAD_A,
                actual_head_sha=HEAD_B,
                stage="context.pre_reconcile",
                phase_claim=_claim(),
                table=self.table,
            )
        )
        new = persistence.get_item(REPO, PR, table=self.table)
        self.assertTrue(persistence.is_valid_head_successor_transition(old, new))

        for field in ("attempt", "context_attempt", "review_attempt"):
            changed = copy.deepcopy(new)
            changed[field] = int(changed.get(field) or 0) + 1
            self.assertFalse(
                persistence.is_valid_head_successor_transition(old, changed),
                field,
            )

        missing = copy.deepcopy(new)
        missing["head_predecessor_receipt"]["provider_call_ids"] = []
        missing["head_predecessor_receipt"]["provider_call_count"] = 0
        self.assertFalse(
            persistence.is_valid_head_successor_transition(old, missing)
        )

        extra = copy.deepcopy(new)
        extra["head_predecessor_receipt"]["provider_call_ids"].append("d" * 64)
        extra["head_predecessor_receipt"]["provider_call_count"] = 2
        self.assertFalse(
            persistence.is_valid_head_successor_transition(old, extra)
        )

        mutated = copy.deepcopy(new)
        mutated[f"{persistence.PROVIDER_CALL_ATTR_PREFIX}{CALL_ID}"][
            "pipeline_attempt"
        ] = 2
        self.assertFalse(
            persistence.is_valid_head_successor_transition(old, mutated)
        )

    def test_successor_never_replaces_an_existing_publication_intent(self):
        self._put_pending(publication_intent={"state": "prepared"})
        self.assertFalse(
            persistence.requeue_head_successor(
                REPO,
                PR,
                expected_status="PENDING",
                expected_head_sha=HEAD_A,
                actual_head_sha=HEAD_B,
                stage="context.ingest",
                phase_claim=_claim(),
                table=self.table,
            )
        )
        current = persistence.get_item(REPO, PR, table=self.table)
        self.assertEqual(current["head_sha"], HEAD_A)
        self.assertEqual(current["publication_intent"], {"state": "prepared"})

    def test_stream_identity_fences_stale_pending_delivery(self):
        self._put_pending(head_sha=HEAD_B, run_id="run-b")
        persistence.get_item(REPO, PR, table=self.table).pop(
            "context_claim", None
        )
        stale = pipeline_admission.claim_phase_delivery(
            REPO,
            PR,
            phase="context",
            expected_status="PENDING",
            runtime_identity={"aws_request_id": "request-2"},
            stream_event_id="old-stream",
            stream_head_sha=HEAD_A,
            stream_run_id="run-a",
            table=self.table,
        )
        self.assertIsNone(stale)

        current = pipeline_admission.claim_phase_delivery(
            REPO,
            PR,
            phase="context",
            expected_status="PENDING",
            runtime_identity={"aws_request_id": "request-2"},
            stream_event_id="successor-stream",
            stream_head_sha=HEAD_B,
            stream_run_id="run-b",
            table=self.table,
        )
        self.assertIsNotNone(current)
        self.assertEqual(current.current_item["head_sha"], HEAD_B)
        self.assertEqual(current.current_item["run_id"], "run-b")


class SuccessorStreamDispatchTests(unittest.TestCase):
    def test_only_exact_successor_same_status_event_dispatches(self):
        old = {
            "repo": REPO,
            "pr_number": PR,
            "status": "PENDING",
            "head_sha": HEAD_A,
            "run_id": "run-a",
        }
        new = {
            **old,
            "head_sha": HEAD_B,
            "run_id": persistence.head_successor_run_id(REPO, PR, HEAD_B),
            "head_successor_count": 1,
            "head_predecessor_receipt": {
                "schema_version": 1,
                "kind": "head_predecessor",
                "outcome": "SUPERSEDED",
                "predecessor_head_sha": HEAD_A,
                "predecessor_run_id": "run-a",
                "successor_head_sha": HEAD_B,
                "successor_run_id": persistence.head_successor_run_id(
                    REPO, PR, HEAD_B
                ),
                "provider_calls_retained_on_item": True,
                "provider_calls_terminal": True,
            },
        }
        with patch.object(
            lambda_function.orchestrator, "run_context_phase"
        ) as run_context:
            lambda_function.process_record(_stream_record(old, new))
            run_context.assert_called_once()

            invalid = {**new, "head_successor_count": 2}
            lambda_function.process_record(_stream_record(new, invalid))
            self.assertEqual(run_context.call_count, 1)


if __name__ == "__main__":
    unittest.main()
