from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from tests.unit.fakes import (
    FakeDynamoResource,
    FakeS3Client,
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

from lambdas.LlamaPReviewPipeline import persistence, pipeline_publication
from lambdas.LlamaPReviewPipeline.deadline import (
    Deadline,
    DeadlineExceeded,
)
from lambdas.LlamaPReviewPipeline.errors import (
    PRLifecycleSuperseded,
    PublicationIntegrityFailure,
    PublicationOutcomeUnknown,
    PublicationPreDispatchAbort,
    PublicationPreflightUnavailable,
    PublicationStateConflict,
)
from lambdas.LlamaPReviewPipeline.provider_accounting import sha256_value
from lambdas.LlamaPReviewPipeline.review.publish import (
    PreparedGitHubReview,
    prepare_main_comment_publication,
)
from lambdas.LlamaPReviewPipeline.review.github_publication_surface import (
    assert_no_existing_bot_review,
    reconcile_dispatching,
)
from lambdas.LlamaPReviewPipeline.review.publication_candidate import (
    build_candidate,
    load_candidate,
    persist_prepared_intent,
    validate_recovery_binding,
)
from lambdas.LlamaPReviewPipeline.review.publication import (
    begin_recovery,
    execute_dispatching,
    mark_dispatching,
    publish_prepared_transaction,
    recover_publication_transaction,
    store_terminal_receipt,
)


HEAD = "abcdef123456"
EVENT = "stream-event-publication"
NOW = datetime.now(timezone.utc).replace(microsecond=0)


def _prepared(*, inline: bool = False) -> PreparedGitHubReview:
    comments = (
        (
            {
                "path": "src/app.py",
                "body": "Use the validated value.",
                "line": 9,
                "side": "RIGHT",
            },
        )
        if inline
        else ()
    )
    return PreparedGitHubReview(
        head_sha=HEAD,
        main_body="### Exact review body",
        comments=comments,
        artifact={
            "main_comment": "### Exact review body",
            "inline_comments": [dict(item) for item in comments],
            "fallback_comments": [],
            "review_mode": "high",
            "publication_status": "not_published",
        },
    )


def _complete_accounting(*, phase: str = "review") -> dict:
    operation = {
        "run_id": "run-7",
        "head_sha": HEAD,
        "pipeline_phase": phase,
        "pipeline_attempt": 1,
        "phase": "deep_judgment" if phase == "review" else "route",
        "call_index": 1,
    }
    operation_id = sha256_value(operation)
    call = {
        **operation,
        "schema_version": 2,
        "operation_id": operation_id,
        "call_id": sha256_value(
            {"operation_id": operation_id, "transport_attempt_index": 1}
        ),
        "model": "deepseek-v4-pro",
        "logical_model": "deepseek-v4-pro",
        "billed_model": "deepseek-v4-flash",
        "status": "completed",
        "usage_state": "reported",
        "transport_attempt_index": 1,
        "transport_dispatch_count": 1,
        "transport_attempt_count": 1,
        "usage": {
            "prompt_tokens": 80,
            "completion_tokens": 20,
            "total_tokens": 100,
        },
    }
    return {
        "deepseek_all_attempt_model_phases": [call],
        "deepseek_model_phases": [deepcopy(call)],
        "deepseek_discarded_model_phases": [],
        "deepseek_usage_total": deepcopy(call["usage"]),
        "deepseek_winning_usage_total": deepcopy(call["usage"]),
        "deepseek_discarded_usage_total": {},
        "deepseek_usage_accounting": {
            "schema_version": 2,
            "all_call_count": 1,
            "winning_call_count": 1,
            "discarded_call_count": 0,
            "transport_operation_count": 1,
            "unreported_usage_call_count": 0,
            "complete_numeric_usage": True,
            "usage_merge_conflicts": [],
        },
    }


def _incomplete_accounting(*, phase: str = "review") -> dict:
    accounting = _complete_accounting(phase=phase)
    for partition in (
        "deepseek_all_attempt_model_phases",
        "deepseek_model_phases",
    ):
        accounting[partition][0]["usage_state"] = "unreported"
        accounting[partition][0]["usage"] = {}
    accounting["deepseek_usage_total"] = {}
    accounting["deepseek_winning_usage_total"] = {}
    accounting["deepseek_usage_accounting"].update(
        {
            "unreported_usage_call_count": 1,
            "complete_numeric_usage": False,
        }
    )
    return accounting


def _candidate(*, phase: str = "review", inline: bool = False) -> dict:
    return build_candidate(
        _prepared(inline=inline),
        repo="owner/repo",
        pr_number=7,
        run_id="run-7",
        phase=phase,
        owner_event_id=EVENT,
        owner_request_id="request-1",
        publication_generation_attempt=1,
        preflight_completed_at=NOW.isoformat(),
        generation_runtime_identity={
            "phase": phase,
            "aws_request_id": "request-1",
        },
        terminal_attributes={
            "run_id": "run-7",
            "pipeline_attempt": 1,
            **_complete_accounting(phase=phase),
        },
        publication_key="a" * 32,
    )


def _intent(candidate: dict, *, state: str = "dispatching") -> dict:
    value = {
        "schema_version": candidate["publication_schema_version"],
        "publication_key": candidate["publication_key"],
        "state": state,
        "repo": candidate["repo"],
        "pr_number": candidate["pr_number"],
        "phase": candidate["phase"],
        "owner_event_id": candidate["owner_event_id"],
        "owner_request_id": candidate["owner_request_id"],
        "run_id": candidate["run_id"],
        "head_sha": candidate["head_sha"],
        "publication_kind": candidate["publication_kind"],
        "required_disposition": candidate["required_disposition"],
        "publication_generation_phase": candidate[
            "publication_generation_phase"
        ],
        "publication_generation_attempt": candidate[
            "publication_generation_attempt"
        ],
        "publication_attempt": 1 if state == "dispatching" else 0,
        "publication_recovery_attempt": 2,
        "payload_sha256": candidate["payload_sha256"],
        "main_body_sha256": candidate["main_body_sha256"],
        "comments_sha256": candidate["comments_sha256"],
        "preflight_completed_at": candidate["preflight_completed_at"],
        "prepared_at": candidate["preflight_completed_at"],
        "generation_runtime_identity": candidate[
            "generation_runtime_identity"
        ],
    }
    if state == "dispatching":
        value["dispatched_at"] = NOW.isoformat()
    if candidate["phase"] == "review":
        value["review_generation_attempt"] = 1
    return value


def _legacy_candidate_and_intent(*, state: str) -> tuple[dict, dict]:
    candidate = _candidate()
    intent = _intent(candidate, state=state)
    candidate["publication_schema_version"] = 1
    candidate.pop("publication_kind")
    candidate.pop("required_disposition")
    candidate["terminal_attributes"].pop("publication_kind", None)
    candidate["terminal_attributes"].pop("required_disposition", None)
    candidate["review_artifact"].pop("publication_kind", None)
    candidate["review_artifact"].pop("required_disposition", None)
    intent["schema_version"] = 1
    intent.pop("publication_kind")
    intent.pop("required_disposition")
    return candidate, intent


def _review(
    prepared: PreparedGitHubReview,
    *,
    review_id: int = 41,
    author: str = "llamapreview[bot]",
    head: str = HEAD,
    body: str | None = None,
    submitted_at: datetime = NOW,
    state: str | None = "COMMENTED",
):
    return SimpleNamespace(
        id=review_id,
        commit_id=head,
        body=prepared.main_body if body is None else body,
        user=SimpleNamespace(login=author),
        submitted_at=submitted_at,
        state=state,
    )


def _comments(prepared: PreparedGitHubReview, review_id: int = 41):
    return [
        SimpleNamespace(
            id=900 + index,
            pull_request_review_id=review_id,
            user=SimpleNamespace(login="llamapreview[bot]"),
            **dict(comment),
        )
        for index, comment in enumerate(prepared.comments, start=1)
    ]


class _Pull:
    def __init__(self):
        self.review_pages: list[object] = [[]]
        self.review_comments: object = []
        self.create_count = 0
        self.create_behavior = None
        self.head = SimpleNamespace(sha=HEAD)
        self.state = "open"
        self.merged = False
        self.locked = False

    def get_reviews(self):
        if len(self.review_pages) > 1:
            return self.review_pages.pop(0)
        return self.review_pages[0]

    def get_review_comments(self):
        return self.review_comments

    def create_review(self, **kwargs):
        self.create_count += 1
        if self.create_behavior is not None:
            return self.create_behavior(kwargs)
        review_id = 41
        created = SimpleNamespace(
            id=review_id,
            commit_id=kwargs["commit"].sha,
            body=kwargs["body"],
            user=SimpleNamespace(login="llamapreview[bot]"),
            submitted_at=datetime.now(timezone.utc).replace(microsecond=0),
            state="COMMENTED",
            raw_data={
                "comments": [
                    {"id": 900 + index}
                    for index, _ in enumerate(
                        kwargs.get("comments") or [], start=1
                    )
                ]
            },
        )
        self.review_pages = [[created]]
        self.review_comments = [
            SimpleNamespace(
                id=900 + index,
                pull_request_review_id=review_id,
                user=SimpleNamespace(login="llamapreview[bot]"),
                **comment,
            )
            for index, comment in enumerate(
                kwargs.get("comments") or [], start=1
            )
        ]
        return created


class _Repo:
    def __init__(self, pull: _Pull):
        self.pull = pull
        self.repo = SimpleNamespace(
            get_pull=lambda _number: pull,
            get_commit=lambda *, sha: SimpleNamespace(sha=sha),
        )


class PublicationReconciliationTests(unittest.TestCase):
    def test_pull_access_failures_are_typed_by_publication_phase(self):
        candidate = _candidate()
        intent = _intent(candidate)
        cases = (
            (SimpleNamespace(), None),
            (
                SimpleNamespace(
                    get_pull=Mock(side_effect=RuntimeError("503"))
                ),
                "getter_error",
            ),
        )
        for native_repo, failure_kind in cases:
            with self.subTest(
                mode="preflight", failure_kind=failure_kind
            ):
                with self.assertRaises(
                    PublicationPreflightUnavailable
                ) as raised:
                    assert_no_existing_bot_review(
                        SimpleNamespace(repo=native_repo), 7
                    )
                self.assertEqual(
                    raised.exception.stage,
                    "publication.preflight.pull",
                )
            with self.subTest(
                mode="reconciliation", failure_kind=failure_kind
            ):
                with self.assertRaises(
                    PublicationOutcomeUnknown
                ) as raised:
                    reconcile_dispatching(
                        SimpleNamespace(repo=native_repo),
                        7,
                        intent=intent,
                        candidate=candidate,
                        max_observations=1,
                    )
                self.assertEqual(
                    raised.exception.stage,
                    "publication.reconcile.pull",
                )

    def test_preflight_rejects_any_existing_bot_review(self):
        pull = _Pull()
        pull.review_pages = [[_review(_prepared())]]
        with self.assertRaises(PublicationIntegrityFailure):
            assert_no_existing_bot_review(_Repo(pull), 7)

    def test_preflight_incomplete_pagination_fails_closed(self):
        pull = _Pull()

        def incomplete():
            yield SimpleNamespace(
                id=1,
                user=SimpleNamespace(login="another-user"),
            )
            raise RuntimeError("next page failed")

        pull.review_pages = [incomplete()]
        with self.assertRaises(PublicationPreflightUnavailable):
            assert_no_existing_bot_review(_Repo(pull), 7)

    def test_zero_then_exact_effect_is_adopted_without_dispatch(self):
        candidate = _candidate(inline=True)
        intent = _intent(candidate)
        prepared = _prepared(inline=True)
        pull = _Pull()
        pull.review_pages = [[], [_review(prepared)]]
        pull.review_comments = _comments(prepared)

        receipt = reconcile_dispatching(
            _Repo(pull),
            7,
            intent=intent,
            candidate=candidate,
            max_observations=2,
            poll_seconds=0,
        )

        self.assertEqual(receipt.outcome, "adopted")
        self.assertEqual(receipt.inline_comment_ids, (901,))
        self.assertEqual(pull.create_count, 0)

    def test_zero_after_window_is_unknown_and_never_dispatches(self):
        candidate = _candidate()
        pull = _Pull()
        with self.assertRaises(PublicationOutcomeUnknown):
            reconcile_dispatching(
                _Repo(pull),
                7,
                intent=_intent(candidate),
                candidate=candidate,
                max_observations=3,
                poll_seconds=0,
            )
        self.assertEqual(pull.create_count, 0)

    def test_reconciliation_stops_before_consuming_deadline_reserve(self):
        candidate = _candidate()
        pull = _Pull()
        deadline = Deadline.for_seconds(0)
        sleeper = Mock()
        with self.assertRaises(DeadlineExceeded):
            reconcile_dispatching(
                _Repo(pull),
                7,
                intent=_intent(candidate),
                candidate=candidate,
                deadline=deadline,
                max_observations=4,
                poll_seconds=0.5,
                sleeper=sleeper,
            )
        sleeper.assert_not_called()
        self.assertEqual(pull.create_count, 0)

    def test_pagination_rechecks_budget_before_each_next_page(self):
        candidate = _candidate()
        prepared = _prepared()
        yielded = []

        def reviews():
            yielded.append(1)
            yield _review(prepared)
            yielded.append(2)
            yield _review(prepared, review_id=42)

        class _PageDeadline:
            def __init__(self):
                self.stage_checks = 0

            def check(self, stage, *, minimum_seconds=0):
                if stage == "publication.reconcile.reviews":
                    self.stage_checks += 1
                    if self.stage_checks == 3:
                        raise DeadlineExceeded(stage)
                return minimum_seconds + 1

            def remaining_seconds(self):
                return 100

        pull = _Pull()
        pull.review_pages = [reviews()]
        with self.assertRaises(DeadlineExceeded):
            reconcile_dispatching(
                _Repo(pull),
                7,
                intent=_intent(candidate),
                candidate=candidate,
                deadline=_PageDeadline(),
                max_observations=1,
            )
        self.assertEqual(yielded, [1])

    def test_accepted_then_timeout_is_exactly_adopted(self):
        candidate = _candidate(inline=True)
        prepared = _prepared(inline=True)
        pull = _Pull()

        def accepted_then_timeout(_kwargs):
            pull.review_pages = [[_review(prepared)]]
            pull.review_comments = _comments(prepared)
            raise TimeoutError("response lost")

        pull.create_behavior = accepted_then_timeout
        receipt = execute_dispatching(
            _Repo(pull),
            7,
            intent=_intent(candidate),
            candidate=candidate,
        )
        self.assertEqual(receipt.outcome, "adopted")
        self.assertEqual(pull.create_count, 1)

    def test_missing_response_id_or_commit_reconciles_without_redispatch(self):
        for missing_field in ("id", "commit_id"):
            with self.subTest(missing_field=missing_field):
                candidate = _candidate()
                prepared = _prepared()
                pull = _Pull()

                def accepted_missing_identity(_kwargs):
                    pull.review_pages = [[_review(prepared)]]
                    response = {
                        "id": 41,
                        "commit_id": HEAD,
                        "raw_data": {"comments": []},
                    }
                    response.pop(missing_field)
                    return SimpleNamespace(**response)

                pull.create_behavior = accepted_missing_identity
                receipt = execute_dispatching(
                    _Repo(pull),
                    7,
                    intent=_intent(candidate),
                    candidate=candidate,
                )
                self.assertEqual(receipt.outcome, "adopted")
                self.assertEqual(pull.create_count, 1)

    def test_synchronous_success_is_created_only_after_exact_surface_proof(self):
        candidate = _candidate(inline=True)
        pull = _Pull()

        receipt = execute_dispatching(
            _Repo(pull),
            7,
            intent=_intent(candidate),
            candidate=candidate,
        )

        self.assertEqual(receipt.outcome, "created")
        self.assertEqual(receipt.review_id, 41)
        self.assertEqual(receipt.inline_comment_ids, (901,))
        self.assertEqual(pull.create_count, 1)

    def test_synchronous_response_cannot_bypass_exact_surface_binding(self):
        candidate = _candidate(inline=True)
        prepared = _prepared(inline=True)
        deadline = SimpleNamespace(
            check=lambda _stage, minimum_seconds=0: (
                minimum_seconds + 1
            ),
            remaining_seconds=lambda: 0.05,
        )
        cases = (
            "body",
            "author",
            "time",
            "inline_body",
            "inline_author",
        )
        for corruption in cases:
            with self.subTest(corruption=corruption):
                pull = _Pull()

                def corrupted_success(_kwargs):
                    review = _review(prepared)
                    comments = _comments(prepared)
                    if corruption == "body":
                        review.body = "WRONG PUBLIC BODY"
                    elif corruption == "author":
                        review.user = SimpleNamespace(login="another-user")
                    elif corruption == "time":
                        review.submitted_at = NOW + timedelta(hours=1)
                    elif corruption == "inline_body":
                        comments[0].body = "wrong inline body"
                    elif corruption == "inline_author":
                        comments[0].user = SimpleNamespace(
                            login="another-user"
                        )
                    pull.review_pages = [[review]]
                    pull.review_comments = comments
                    return review

                pull.create_behavior = corrupted_success
                with self.assertRaises(
                    (
                        PublicationIntegrityFailure,
                        PublicationOutcomeUnknown,
                    )
                ):
                    execute_dispatching(
                        _Repo(pull),
                        7,
                        intent=_intent(candidate),
                        candidate=candidate,
                        deadline=deadline,
                    )
                self.assertEqual(pull.create_count, 1)

    def test_reconciled_review_and_inline_ids_are_typed(self):
        candidate = _candidate(inline=True)
        prepared = _prepared(inline=True)
        for invalid_surface in ("review_id", "inline_id"):
            with self.subTest(invalid_surface=invalid_surface):
                pull = _Pull()
                review = _review(prepared)
                comments = _comments(prepared)
                if invalid_surface == "review_id":
                    review.id = True
                else:
                    comments[0].id = False
                pull.review_pages = [[review]]
                pull.review_comments = comments
                with self.assertRaises(PublicationIntegrityFailure):
                    reconcile_dispatching(
                        _Repo(pull),
                        7,
                        intent=_intent(candidate),
                        candidate=candidate,
                        max_observations=1,
                    )

    def test_accepted_generic_503_is_reconciled(self):
        candidate = _candidate()
        prepared = _prepared()
        pull = _Pull()

        def accepted_503(_kwargs):
            pull.review_pages = [[_review(prepared)]]
            error = RuntimeError("transport response unavailable")
            error.status = 503
            raise error

        pull.create_behavior = accepted_503
        receipt = execute_dispatching(
            _Repo(pull),
            7,
            intent=_intent(candidate),
            candidate=candidate,
        )
        self.assertEqual(receipt.outcome, "adopted")
        self.assertEqual(pull.create_count, 1)

    def test_definite_422_does_not_enter_reconciliation(self):
        candidate = _candidate()
        pull = _Pull()

        def rejected(_kwargs):
            error = RuntimeError("request rejected")
            error.status = 422
            raise error

        pull.create_behavior = rejected
        with self.assertRaises(RuntimeError):
            execute_dispatching(
                _Repo(pull),
                7,
                intent=_intent(candidate),
                candidate=candidate,
            )
        self.assertEqual(pull.create_count, 1)

    def test_multiple_exact_effects_fail_integrity(self):
        candidate = _candidate()
        prepared = _prepared()
        pull = _Pull()
        pull.review_pages = [[
            _review(prepared, review_id=41),
            _review(prepared, review_id=42),
        ]]
        with self.assertRaises(PublicationIntegrityFailure):
            reconcile_dispatching(
                _Repo(pull),
                7,
                intent=_intent(candidate),
                candidate=candidate,
                max_observations=1,
            )

    def test_wrong_author_is_never_adopted(self):
        candidate = _candidate()
        pull = _Pull()
        pull.review_pages = [[
            _review(_prepared(), author="another-user")
        ]]
        with self.assertRaises(PublicationOutcomeUnknown):
            reconcile_dispatching(
                _Repo(pull),
                7,
                intent=_intent(candidate),
                candidate=candidate,
                max_observations=1,
            )

    def test_wrong_head_body_or_time_fails_integrity(self):
        prepared = _prepared()
        cases = (
            {"head": "wrong-head"},
            {"body": "wrong body"},
            {
                "submitted_at": NOW
                - timedelta(seconds=61)
            },
            {
                "submitted_at": NOW
                + timedelta(hours=1)
            },
        )
        for changes in cases:
            with self.subTest(changes=changes):
                candidate = _candidate()
                pull = _Pull()
                pull.review_pages = [[_review(prepared, **changes)]]
                with self.assertRaises(PublicationIntegrityFailure):
                    reconcile_dispatching(
                        _Repo(pull),
                        7,
                        intent=_intent(candidate),
                        candidate=candidate,
                        max_observations=1,
                    )

    def test_non_comment_review_state_is_never_adopted(self):
        candidate = _candidate()
        prepared = _prepared()
        for state in ("APPROVED", "CHANGES_REQUESTED", None):
            with self.subTest(state=state):
                pull = _Pull()
                pull.review_pages = [[_review(prepared, state=state)]]
                with self.assertRaises(PublicationIntegrityFailure):
                    reconcile_dispatching(
                        _Repo(pull),
                        7,
                        intent=_intent(candidate),
                        candidate=candidate,
                        max_observations=1,
                    )

    def test_same_second_and_clock_skew_boundary_are_adopted(self):
        prepared = _prepared()
        for submitted_at in (
            NOW,
            NOW - timedelta(seconds=60),
            NOW + timedelta(seconds=60),
        ):
            with self.subTest(submitted_at=submitted_at):
                candidate = _candidate()
                pull = _Pull()
                pull.review_pages = [[
                    _review(prepared, submitted_at=submitted_at)
                ]]
                receipt = reconcile_dispatching(
                    _Repo(pull),
                    7,
                    intent=_intent(candidate),
                    candidate=candidate,
                    max_observations=1,
                )
                self.assertEqual(receipt.outcome, "adopted")

    def test_wrong_inline_payload_or_author_fails_integrity(self):
        candidate = _candidate(inline=True)
        prepared = _prepared(inline=True)
        for mutate in ("body", "author", "parent"):
            with self.subTest(mutate=mutate):
                pull = _Pull()
                pull.review_pages = [[_review(prepared)]]
                comments = _comments(prepared)
                if mutate == "body":
                    comments[0].body = "different"
                else:
                    if mutate == "author":
                        comments[0].user.login = "another-user"
                    else:
                        comments[0].pull_request_review_id = 999
                pull.review_comments = comments
                with self.assertRaises(PublicationIntegrityFailure):
                    reconcile_dispatching(
                        _Repo(pull),
                        7,
                        intent=_intent(candidate),
                        candidate=candidate,
                        max_observations=1,
                    )

    def test_incomplete_review_pagination_fails_closed(self):
        candidate = _candidate()
        prepared = _prepared()
        pull = _Pull()

        def incomplete():
            yield _review(prepared)
            raise RuntimeError("next page failed")

        pull.review_pages = [incomplete()]
        with self.assertRaises(PublicationOutcomeUnknown):
            reconcile_dispatching(
                _Repo(pull),
                7,
                intent=_intent(candidate),
                candidate=candidate,
                max_observations=1,
            )

    def test_incomplete_review_comment_pagination_never_adopts(self):
        candidate = _candidate(inline=True)
        prepared = _prepared(inline=True)
        pull = _Pull()
        pull.review_pages = [[_review(prepared)]]

        def incomplete_comments():
            yield _comments(prepared)[0]
            raise RuntimeError("next review-comment page failed")

        pull.review_comments = incomplete_comments()
        with self.assertRaises(PublicationOutcomeUnknown) as raised:
            reconcile_dispatching(
                _Repo(pull),
                7,
                intent=_intent(candidate),
                candidate=candidate,
                max_observations=1,
            )
        self.assertEqual(
            raised.exception.stage,
            "publication.reconcile.comments",
        )


class PublicationTransactionTests(unittest.TestCase):
    def setUp(self):
        self.table = fake_dynamo.Table("llamapreview-pipeline-test")
        self.table.reset()
        self.s3 = FakeS3Client()
        self.pull = _Pull()
        self.repo = _Repo(self.pull)

    def _put_item(self, *, phase="review"):
        status = "CONTEXT_READY" if phase == "review" else "PENDING"
        self.table.put_item(
            Item={
                "repo": "owner/repo",
                "pr_number": 7,
                "status": status,
                "head_sha": HEAD,
                "run_id": "run-7",
            }
        )
        return status

    def _claim(
        self,
        phase="review",
        *,
        request="request-1",
    ):
        return persistence.claim_phase_attempt(
            "owner/repo",
            7,
            phase,
            expected_status=(
                "CONTEXT_READY" if phase == "review" else "PENDING"
            ),
            runtime_identity={
                "phase": phase,
                "aws_request_id": request,
            },
            stream_event_id=EVENT,
            table=self.table,
        )

    def _publish(self, *, phase="review", inline=False):
        claim = self._claim(phase)
        with patch.object(
            persistence, "get_s3_client", return_value=self.s3
        ):
            stored = publish_prepared_transaction(
                _prepared(inline=inline),
                repo_obj=self.repo,
                repo="owner/repo",
                pr_number=7,
                expected_status=(
                    "CONTEXT_READY" if phase == "review" else "PENDING"
                ),
                run_id="run-7",
                phase=phase,
                generation_attempt=1,
                runtime_identity={
                    "phase": phase,
                    "aws_request_id": "request-1",
                },
                terminal_attributes={
                    "run_id": "run-7",
                    "pipeline_attempt": 1,
                    **_complete_accounting(phase=phase),
                },
                pre_publish_check=lambda: None,
                phase_claim=claim,
                table=self.table,
            )
        return stored

    def test_first_transaction_locked_after_intent_aborts_exactly_once(self):
        self._put_item()
        claim = self._claim()
        ordinary = _prepared()
        prepared = PreparedGitHubReview(
            head_sha=ordinary.head_sha,
            main_body=ordinary.main_body,
            comments=ordinary.comments,
            artifact=ordinary.artifact,
            publication_kind="post_merge_follow_up",
            required_disposition="merged_same_head",
        )
        context = pipeline_publication.PublicationContext(
            repo="owner/repo",
            pr_number=7,
            head_sha=HEAD,
            expected_status="CONTEXT_READY",
            phase="review",
            run_id="run-7",
            generation_attempt=1,
            runtime_identity={"phase": "review"},
            phase_claim=claim,
            dry_run=False,
            publication_kind="post_merge_follow_up",
            required_disposition="merged_same_head",
        )
        stopped = PRLifecycleSuperseded(
            HEAD,
            HEAD,
            current_state="closed",
            merged=True,
            stage="publication.pre_publish_disposition",
            superseded_kind="publication_unavailable_locked",
        )
        pre_publish = Mock(side_effect=[None, stopped])
        observer = Mock()

        with patch.object(
            persistence,
            "get_s3_client",
            return_value=self.s3,
        ), patch.object(
            persistence,
            "load_publication_candidate",
            side_effect=AssertionError(
                "first transaction must reuse its in-memory candidate"
            ),
        ):
            stored = publish_prepared_transaction(
                prepared,
                repo_obj=self.repo,
                repo="owner/repo",
                pr_number=7,
                expected_status="CONTEXT_READY",
                run_id="run-7",
                phase="review",
                generation_attempt=1,
                runtime_identity={"phase": "review"},
                terminal_attributes=_complete_accounting(),
                pre_publish_check=pre_publish,
                phase_claim=claim,
                prepared_pre_dispatch_failure_commit=(
                    lambda candidate, intent, exc: (
                        pipeline_publication._commit_prepared_locked_unavailable(
                            candidate,
                            intent,
                            exc,
                            context=context,
                            lifecycle_unavailable_observer=observer,
                            table=self.table,
                        )
                    )
                ),
                table=self.table,
            )

        self.assertTrue(stored)
        self.assertEqual(pre_publish.call_count, 2)
        self.assertEqual(self.pull.create_count, 0)
        observer.assert_called_once()
        terminal = persistence.get_item("owner/repo", 7, table=self.table)
        self.assertEqual(terminal["status"], "SUPERSEDED")
        self.assertEqual(
            terminal["publication_status"],
            "aborted_before_dispatch",
        )
        self.assertTrue(
            terminal["deepseek_usage_accounting"]["complete_numeric_usage"]
        )

    def test_fresh_close_after_dispatching_mark_commits_known_zero_write_abort(self):
        self._put_item()
        claim = self._claim()
        context = pipeline_publication.PublicationContext(
            repo="owner/repo",
            pr_number=7,
            head_sha=HEAD,
            expected_status="CONTEXT_READY",
            phase="review",
            run_id="run-7",
            generation_attempt=1,
            runtime_identity={"phase": "review"},
            phase_claim=claim,
            dry_run=False,
        )
        self.pull.state = "closed"
        self.pull.merged = False

        with patch.object(
            persistence,
            "get_s3_client",
            return_value=self.s3,
        ):
            stored = publish_prepared_transaction(
                _prepared(),
                repo_obj=self.repo,
                repo="owner/repo",
                pr_number=7,
                expected_status="CONTEXT_READY",
                run_id="run-7",
                phase="review",
                generation_attempt=1,
                runtime_identity={"phase": "review"},
                terminal_attributes=_complete_accounting(),
                pre_publish_check=lambda: None,
                phase_claim=claim,
                prepared_pre_dispatch_failure_commit=(
                    lambda candidate, intent, exc: (
                        pipeline_publication._commit_known_zero_write_abort(
                            candidate,
                            intent,
                            exc,
                            context=context,
                            lifecycle_unavailable_observer=None,
                            table=self.table,
                        )
                    )
                ),
                table=self.table,
            )

        self.assertTrue(stored)
        self.assertEqual(self.pull.create_count, 0)
        terminal = persistence.get_item("owner/repo", 7, table=self.table)
        self.assertEqual(terminal["status"], "SUPERSEDED")
        self.assertEqual(terminal["publication_status"], "aborted_before_dispatch")
        self.assertIs(terminal["publication_post_started"], False)
        self.assertEqual(terminal["publication_pre_dispatch_abort"], "pr_closed")
        self.assertNotIn("publication_receipt", terminal)

    def test_artifact_or_intent_failure_performs_zero_github_writes(self):
        for failing_step in ("artifact", "intent"):
            with self.subTest(failing_step=failing_step):
                self.table.reset()
                self._put_item()
                claim = self._claim()
                target = (
                    "store_publication_candidate"
                    if failing_step == "artifact"
                    else "store_publication_intent"
                )
                effect = (
                    RuntimeError("artifact unavailable")
                    if failing_step == "artifact"
                    else False
                )
                with patch.object(
                    persistence, "get_s3_client", return_value=self.s3
                ), patch.object(persistence, target, side_effect=effect):
                    with self.assertRaises(Exception):
                        publish_prepared_transaction(
                            _prepared(),
                            repo_obj=self.repo,
                            repo="owner/repo",
                            pr_number=7,
                            expected_status="CONTEXT_READY",
                            run_id="run-7",
                            phase="review",
                            generation_attempt=1,
                            runtime_identity={"phase": "review"},
                            terminal_attributes=_complete_accounting(),
                            pre_publish_check=lambda: None,
                            phase_claim=claim,
                            table=self.table,
                        )
                self.assertEqual(self.pull.create_count, 0)

    def test_incomplete_usage_is_preserved_without_blocking_publication(self):
        self._put_item()
        claim = self._claim()
        with patch.object(
            persistence, "get_s3_client", return_value=self.s3
        ):
            self.assertTrue(
                publish_prepared_transaction(
                    _prepared(),
                    repo_obj=self.repo,
                    repo="owner/repo",
                    pr_number=7,
                    expected_status="CONTEXT_READY",
                    run_id="run-7",
                    phase="review",
                    generation_attempt=1,
                    runtime_identity={"phase": "review"},
                    terminal_attributes=_incomplete_accounting(),
                    pre_publish_check=lambda: None,
                    phase_claim=claim,
                    table=self.table,
                )
            )

        current = persistence.get_item("owner/repo", 7, table=self.table)
        self.assertEqual(current["status"], "PROCESSED")
        self.assertEqual(current["publication_status"], "published")
        self.assertEqual(self.pull.create_count, 1)
        self.assertFalse(
            current["deepseek_usage_accounting"]["complete_numeric_usage"]
        )
        self.assertEqual(
            current["deepseek_usage_accounting"][
                "unreported_usage_call_count"
            ],
            1,
        )

    def test_recovery_publishes_prepared_candidate_with_incomplete_usage(self):
        self._put_item()
        claim = self._claim()
        candidate = _candidate()
        candidate["terminal_attributes"].update(_incomplete_accounting())
        with patch.object(
            persistence, "get_s3_client", return_value=self.s3
        ):
            persist_prepared_intent(
                candidate,
                expected_status="CONTEXT_READY",
                phase_claim=claim,
                table=self.table,
            )
            recovery_claim = self._claim(request="request-2")
            current = persistence.get_item(
                "owner/repo", 7, table=self.table, consistent_read=True
            )
            self.assertTrue(
                recover_publication_transaction(
                    current_item=current,
                    expected_status="CONTEXT_READY",
                    phase_claim=recovery_claim,
                    recovery_runtime_identity={
                        "phase": "review",
                        "aws_request_id": "request-2",
                    },
                    repository_for=lambda _repo: self.repo,
                    pre_publish_check_for=lambda _candidate: (lambda: None),
                    table=self.table,
                )
            )

        current = persistence.get_item("owner/repo", 7, table=self.table)
        self.assertEqual(self.pull.create_count, 1)
        self.assertEqual(current["status"], "PROCESSED")
        self.assertFalse(
            current["deepseek_usage_accounting"]["complete_numeric_usage"]
        )

    def test_v1_prepared_candidate_recovers_once_as_ordinary_open_review(self):
        self._put_item()
        claim = self._claim()
        candidate, intent = _legacy_candidate_and_intent(state="prepared")
        current = persistence.get_item("owner/repo", 7, table=self.table)
        current["publication_intent"] = intent
        self.table.put_item(Item=current)

        with patch.object(
            persistence,
            "load_publication_candidate",
            return_value=candidate,
        ):
            recovered = recover_publication_transaction(
                current_item=current,
                expected_status="CONTEXT_READY",
                phase_claim=claim,
                recovery_runtime_identity={
                    "phase": "review",
                    "aws_request_id": "request-1",
                },
                repository_for=lambda _repo: self.repo,
                pre_publish_check_for=lambda loaded: (
                    lambda: self.assertEqual(
                        loaded["publication_kind"], "ordinary_review"
                    )
                ),
                table=self.table,
            )

        self.assertTrue(recovered)
        self.assertEqual(self.pull.create_count, 1)
        stored = persistence.get_item("owner/repo", 7, table=self.table)
        self.assertEqual(stored["status"], "PROCESSED")

    def test_v1_dispatching_candidate_only_adopts_existing_exact_effect(self):
        self._put_item()
        claim = self._claim()
        candidate, intent = _legacy_candidate_and_intent(state="dispatching")
        current = persistence.get_item("owner/repo", 7, table=self.table)
        current["publication_intent"] = intent
        self.table.put_item(Item=current)
        self.pull.review_pages = [[_review(_prepared())]]

        with patch.object(
            persistence,
            "load_publication_candidate",
            return_value=candidate,
        ):
            recovered = recover_publication_transaction(
                current_item=current,
                expected_status="CONTEXT_READY",
                phase_claim=claim,
                recovery_runtime_identity={
                    "phase": "review",
                    "aws_request_id": "request-1",
                },
                repository_for=lambda _repo: self.repo,
                pre_publish_check_for=lambda _candidate: (
                    lambda: self.fail("dispatching recovery must not preflight")
                ),
                table=self.table,
            )

        self.assertTrue(recovered)
        self.assertEqual(self.pull.create_count, 0)
        stored = persistence.get_item("owner/repo", 7, table=self.table)
        self.assertEqual(stored["status"], "PROCESSED")
        self.assertEqual(stored["publication_receipt"]["outcome"], "adopted")

    def test_v1_prepared_candidate_that_is_now_merged_performs_zero_writes(self):
        self._put_item()
        claim = self._claim()
        candidate, intent = _legacy_candidate_and_intent(state="prepared")
        current = persistence.get_item("owner/repo", 7, table=self.table)
        current["publication_intent"] = intent
        self.table.put_item(Item=current)
        runtime = SimpleNamespace(
            get_pr_head_snapshot=lambda _repo, _pr: {
                "head_sha": HEAD,
                "state": "closed",
                "merged": True,
                "locked": False,
            },
            get_repository=lambda _repo: self.repo,
        )
        context = pipeline_publication.PublicationContext(
            repo="owner/repo",
            pr_number=7,
            head_sha=HEAD,
            expected_status="CONTEXT_READY",
            phase="review",
            run_id="run-7",
            generation_attempt=1,
            runtime_identity={"phase": "review"},
            phase_claim=claim,
            dry_run=False,
        )

        with patch.object(
            persistence,
            "load_publication_candidate",
            return_value=candidate,
        ), patch.object(
            pipeline_publication,
            "fetch_pr_details",
            return_value=({}, []),
        ):
            recovered = pipeline_publication.recover_pending(
                current,
                context=context,
                runtime=runtime,
                deadline=None,
                table=self.table,
            )

        self.assertTrue(recovered)
        self.assertEqual(self.pull.create_count, 0)
        terminal = persistence.get_item("owner/repo", 7, table=self.table)
        self.assertEqual(terminal["status"], "SUPERSEDED")
        self.assertEqual(terminal["publication_status"], "aborted_before_dispatch")
        self.assertIs(terminal["publication_post_started"], False)
        self.assertEqual(terminal["publication_pre_dispatch_abort"], "pr_merged")

    def test_locked_post_merge_prepared_recovery_aborts_before_dispatch(self):
        self._put_item()
        claim = self._claim()
        ordinary = _prepared()
        prepared = PreparedGitHubReview(
            head_sha=ordinary.head_sha,
            main_body=ordinary.main_body,
            comments=ordinary.comments,
            artifact=ordinary.artifact,
            publication_kind="post_merge_follow_up",
            required_disposition="merged_same_head",
        )
        candidate = build_candidate(
            prepared,
            repo="owner/repo",
            pr_number=7,
            run_id="run-7",
            phase="review",
            owner_event_id=EVENT,
            owner_request_id="request-1",
            publication_generation_attempt=1,
            preflight_completed_at=NOW.isoformat(),
            generation_runtime_identity={"phase": "review"},
            terminal_attributes={
                "publication_kind": "post_merge_follow_up",
                "required_disposition": "merged_same_head",
                **_complete_accounting(),
            },
            publication_key="b" * 32,
        )
        intent = _intent(candidate, state="prepared")
        current = persistence.get_item("owner/repo", 7, table=self.table)
        current["publication_intent"] = intent
        current["initial_admission"] = {
            "schema_version": 1,
            "disposition": "open_same_head",
            "head_sha": HEAD,
            "run_id": "run-7",
            "admitted_at": NOW.isoformat(),
        }
        self.table.put_item(Item=current)
        runtime = SimpleNamespace(
            get_pr_head_snapshot=lambda _repo, _pr: {
                "head_sha": HEAD,
                "state": "closed",
                "merged": True,
                "locked": True,
            },
            get_repository=Mock(
                side_effect=AssertionError(
                    "locked prepared recovery must not acquire repository"
                )
            ),
        )
        context = pipeline_publication.PublicationContext(
            repo="owner/repo",
            pr_number=7,
            head_sha=HEAD,
            expected_status="CONTEXT_READY",
            phase="review",
            run_id="run-7",
            generation_attempt=1,
            runtime_identity={"phase": "review"},
            phase_claim=claim,
            dry_run=False,
        )

        observer = Mock()
        with patch.object(
            persistence,
            "load_publication_candidate",
            return_value=candidate,
        ) as load_candidate_mock, patch.object(
            pipeline_publication,
            "fetch_pr_details",
        ) as fetch:
            recovered = pipeline_publication.recover_pending(
                current,
                context=context,
                runtime=runtime,
                deadline=None,
                lifecycle_unavailable_observer=observer,
                table=self.table,
            )

        self.assertTrue(recovered)
        self.assertEqual(self.pull.create_count, 0)
        runtime.get_repository.assert_not_called()
        fetch.assert_not_called()
        observer.assert_called_once()
        load_candidate_mock.assert_called_once()
        terminal = persistence.get_item("owner/repo", 7, table=self.table)
        self.assertEqual(terminal["status"], "SUPERSEDED")
        self.assertEqual(
            terminal["superseded_kind"],
            "publication_unavailable_locked",
        )
        self.assertEqual(
            terminal["publication_status"],
            "aborted_before_dispatch",
        )
        self.assertTrue(
            terminal["deepseek_usage_accounting"]["complete_numeric_usage"]
        )

    def test_locked_terminal_cas_rejects_replaced_intent(self):
        self._put_item()
        claim = self._claim()
        intent_a = {
            "state": "prepared",
            "publication_key": "a" * 32,
        }
        intent_b = {
            "state": "prepared",
            "publication_key": "b" * 32,
        }
        current = persistence.get_item("owner/repo", 7, table=self.table)
        current["publication_intent"] = intent_b
        self.table.put_item(Item=current)

        stored = persistence.mark_superseded(
            "owner/repo",
            7,
            "CONTEXT_READY",
            expected_head_sha=HEAD,
            actual_head_sha=HEAD,
            stage="publication.pre_dispatch",
            superseded_kind="publication_unavailable_locked",
            current_state="closed",
            merged=True,
            extra_attrs={"publication_unavailable_locked": True},
            phase_claim=claim,
            expected_publication_intent=intent_a,
            table=self.table,
        )

        self.assertFalse(stored)
        latest = persistence.get_item("owner/repo", 7, table=self.table)
        self.assertEqual(latest["status"], "CONTEXT_READY")
        self.assertEqual(latest["publication_intent"], intent_b)

    def test_intent_and_artifact_bind_all_publication_identity(self):
        self._put_item()
        claim = self._claim()
        candidate = _candidate(inline=True)
        with patch.object(
            persistence, "get_s3_client", return_value=self.s3
        ):
            intent = persist_prepared_intent(
                candidate,
                expected_status="CONTEXT_READY",
                phase_claim=claim,
                table=self.table,
            )
            loaded = load_candidate(intent)
            for field in (
                "repo",
                "pr_number",
                "phase",
                "owner_event_id",
                "head_sha",
                "run_id",
                "payload_sha256",
                "main_body_sha256",
                "comments_sha256",
                "publication_generation_attempt",
                "preflight_completed_at",
            ):
                self.assertEqual(intent[field], candidate[field])
                self.assertEqual(loaded[field], candidate[field])
            self.assertEqual(
                intent["candidate_artifact_sha256"],
                intent["candidate_artifact"]["sha256"],
            )
            tampered = {**intent, "comments_sha256": "0" * 64}
            with self.assertRaises(persistence.ArtifactIntegrityError):
                load_candidate(tampered)

    def test_recovery_binding_rejects_tampered_preflight_time(self):
        self._put_item()
        claim = self._claim()
        candidate = _candidate()
        with patch.object(
            persistence, "get_s3_client", return_value=self.s3
        ):
            intent = persist_prepared_intent(
                candidate,
                expected_status="CONTEXT_READY",
                phase_claim=claim,
                table=self.table,
            )
            tampered = {
                **intent,
                "preflight_completed_at": (
                    NOW - timedelta(days=1)
                ).isoformat(),
            }
            current = persistence.get_item(
                "owner/repo", 7, table=self.table
            )
            current["publication_intent"] = tampered
            loaded = load_candidate(tampered)
            with self.assertRaises(PublicationIntegrityFailure):
                validate_recovery_binding(
                    current_item=current,
                    intent=tampered,
                    candidate=loaded,
                    expected_status="CONTEXT_READY",
                    phase_claim=claim,
                )

    def test_context_body_only_and_review_inline_share_one_transaction(self):
        for phase, inline in (("context", False), ("review", True)):
            with self.subTest(phase=phase):
                self.table.reset()
                self.pull = _Pull()
                self.repo = _Repo(self.pull)
                self._put_item(phase=phase)
                self.assertTrue(self._publish(phase=phase, inline=inline))
                current = persistence.get_item(
                    "owner/repo", 7, table=self.table
                )
                self.assertEqual(current["status"], "PROCESSED")
                self.assertEqual(self.pull.create_count, 1)
                self.assertEqual(
                    len(current["github_inline_comment_ids"]),
                    1 if inline else 0,
                )
                self.assertEqual(
                    current["publication_generation_phase"], phase
                )

    def test_post_write_observation_yields_to_terminal_receipt_deadline(self):
        self._put_item()
        claim = self._claim()
        observer_runtime = Mock()

        class _Deadline:
            def check(self, stage, *, minimum_seconds=0):
                if stage == "publication.post_write_observation":
                    raise DeadlineExceeded(stage)
                return minimum_seconds + 1

            def remaining_seconds(self):
                return 100

        deadline = _Deadline()
        with patch.object(
            persistence, "get_s3_client", return_value=self.s3
        ):
            stored = publish_prepared_transaction(
                _prepared(),
                repo_obj=self.repo,
                repo="owner/repo",
                pr_number=7,
                expected_status="CONTEXT_READY",
                run_id="run-7",
                phase="review",
                generation_attempt=1,
                runtime_identity={"phase": "review"},
                terminal_attributes={
                    "run_id": "run-7",
                    **_complete_accounting(),
                },
                pre_publish_check=lambda: None,
                phase_claim=claim,
                post_write_observation=lambda candidate: (
                    pipeline_publication.post_publication_observation(
                        runtime=observer_runtime,
                        repo=str(candidate.get("repo") or ""),
                        pr_number=int(
                            candidate.get("pr_number") or 0
                        ),
                        expected_head_sha=str(
                            candidate.get("head_sha") or ""
                        ),
                        deadline=deadline,
                    )
                ),
                deadline=deadline,
                table=self.table,
            )

        self.assertTrue(stored)
        self.assertEqual(observer_runtime.mock_calls, [])
        terminal = persistence.get_item(
            "owner/repo", 7, table=self.table
        )
        self.assertEqual(terminal["status"], "PROCESSED")
        self.assertEqual(
            terminal["publication_post_write_observation"],
            "skipped_deadline",
        )

    def test_prepared_recovery_rechecks_then_dispatches_exactly_once(self):
        self._put_item()
        claim = self._claim()
        candidate = _candidate()
        with patch.object(
            persistence, "get_s3_client", return_value=self.s3
        ):
            persist_prepared_intent(
                candidate,
                expected_status="CONTEXT_READY",
                phase_claim=claim,
                table=self.table,
            )
            recovery_claim = self._claim(request="request-2")
            current = persistence.get_item(
                "owner/repo", 7, table=self.table, consistent_read=True
            )
            checks = []
            self.assertTrue(
                recover_publication_transaction(
                    current_item=current,
                    expected_status="CONTEXT_READY",
                    phase_claim=recovery_claim,
                    recovery_runtime_identity={
                        "phase": "review",
                        "aws_request_id": "request-2",
                    },
                    repository_for=lambda _repo: self.repo,
                    pre_publish_check_for=lambda _candidate: (
                        lambda: checks.append("checked")
                    ),
                    table=self.table,
                )
            )
        terminal = persistence.get_item("owner/repo", 7, table=self.table)
        self.assertEqual(checks, ["checked"])
        self.assertEqual(self.pull.create_count, 1)
        self.assertEqual(terminal["review_attempt"], 2)
        self.assertEqual(terminal["review_generation_attempt"], 1)
        self.assertEqual(terminal["publication_generation_attempt"], 1)
        self.assertEqual(terminal["publication_recovery_attempt"], 2)

    def test_dry_run_flip_suppresses_prepared_live_intent_without_github(self):
        self._put_item()
        first_claim = self._claim()
        candidate = _candidate()
        runtime = SimpleNamespace(get_repository=Mock())
        with patch.object(
            persistence, "get_s3_client", return_value=self.s3
        ):
            persist_prepared_intent(
                candidate,
                expected_status="CONTEXT_READY",
                phase_claim=first_claim,
                table=self.table,
            )
            recovery_claim = self._claim(request="request-2")
            current = persistence.get_item(
                "owner/repo",
                7,
                table=self.table,
                consistent_read=True,
            )
            recovered = pipeline_publication.recover_pending(
                current,
                context=pipeline_publication.PublicationContext(
                    repo="owner/repo",
                    pr_number=7,
                    head_sha=HEAD,
                    expected_status="CONTEXT_READY",
                    phase="review",
                    run_id="run-7",
                    generation_attempt=2,
                    runtime_identity={"aws_request_id": "request-2"},
                    phase_claim=recovery_claim,
                    dry_run=True,
                ),
                runtime=runtime,
                deadline=None,
                table=self.table,
            )

        self.assertTrue(recovered)
        runtime.get_repository.assert_not_called()
        self.assertEqual(self.pull.create_count, 0)
        terminal = persistence.get_item(
            "owner/repo", 7, table=self.table
        )
        self.assertEqual(terminal["status"], "PROCESSED_DRYRUN")
        self.assertEqual(
            terminal["publication_status"], "suppressed_dry_run"
        )
        self.assertEqual(
            terminal["publication_suppressed_reason"],
            "dry_run_enabled_before_dispatch",
        )
        self.assertNotIn("publication_receipt", terminal)

    def test_dispatching_recovery_only_reconciles_and_adopts(self):
        self._put_item()
        claim = self._claim()
        candidate = _candidate(inline=True)
        with patch.object(
            persistence, "get_s3_client", return_value=self.s3
        ):
            intent = persist_prepared_intent(
                candidate,
                expected_status="CONTEXT_READY",
                phase_claim=claim,
                table=self.table,
            )
            mark_dispatching(
                repo="owner/repo",
                pr_number=7,
                expected_status="CONTEXT_READY",
                intent=intent,
                phase_claim=claim,
                table=self.table,
            )
            prepared = _prepared(inline=True)
            self.pull.review_pages = [[_review(prepared)]]
            self.pull.review_comments = _comments(prepared)
            recovery_claim = self._claim(request="request-2")
            current = persistence.get_item(
                "owner/repo", 7, table=self.table, consistent_read=True
            )
            self.assertTrue(
                recover_publication_transaction(
                    current_item=current,
                    expected_status="CONTEXT_READY",
                    phase_claim=recovery_claim,
                    recovery_runtime_identity={
                        "phase": "review",
                        "aws_request_id": "request-2",
                    },
                    repository_for=lambda _repo: self.repo,
                    pre_publish_check_for=lambda _candidate: (
                        lambda: self.fail("dispatching must not preflight")
                    ),
                    table=self.table,
                )
            )
        terminal = persistence.get_item("owner/repo", 7, table=self.table)
        self.assertEqual(self.pull.create_count, 0)
        self.assertEqual(
            terminal["publication_receipt"]["outcome"], "adopted"
        )
        self.assertEqual(terminal["publication_recovery_attempt"], 2)

    def test_recovery_binding_rejects_stale_current_head(self):
        self._put_item()
        claim = self._claim()
        candidate = _candidate()
        with patch.object(
            persistence, "get_s3_client", return_value=self.s3
        ):
            persist_prepared_intent(
                candidate,
                expected_status="CONTEXT_READY",
                phase_claim=claim,
                table=self.table,
            )
            recovery_claim = self._claim(request="request-2")
            current = persistence.get_item(
                "owner/repo", 7, table=self.table, consistent_read=True
            )
            current["head_sha"] = "new-head"
            with self.assertRaises(PublicationStateConflict):
                recover_publication_transaction(
                    current_item=current,
                    expected_status="CONTEXT_READY",
                    phase_claim=recovery_claim,
                    recovery_runtime_identity={"phase": "review"},
                    repository_for=lambda _repo: self.repo,
                    pre_publish_check_for=lambda _candidate: lambda: None,
                    table=self.table,
                )
        self.assertEqual(self.pull.create_count, 0)

    def test_terminal_receipt_is_idempotent_only_for_the_exact_receipt(self):
        self._put_item()
        claim = self._claim()
        candidate = _candidate()
        with patch.object(
            persistence, "get_s3_client", return_value=self.s3
        ):
            intent = persist_prepared_intent(
                candidate,
                expected_status="CONTEXT_READY",
                phase_claim=claim,
                table=self.table,
            )
            intent = mark_dispatching(
                repo="owner/repo",
                pr_number=7,
                expected_status="CONTEXT_READY",
                intent=intent,
                phase_claim=claim,
                table=self.table,
            )
            receipt = execute_dispatching(
                self.repo,
                7,
                intent=intent,
                candidate=candidate,
            )
            forged = deepcopy(receipt)
            object.__setattr__(forged, "commit_id", "wrong-head")
            with self.assertRaises(PublicationIntegrityFailure):
                store_terminal_receipt(
                    candidate=candidate,
                    intent=intent,
                    receipt=forged,
                    expected_status="CONTEXT_READY",
                    phase_claim=claim,
                    table=self.table,
                )
            self.assertEqual(
                persistence.get_item(
                    "owner/repo", 7, table=self.table
                )["status"],
                "CONTEXT_READY",
            )
            with self.assertRaises(PublicationIntegrityFailure):
                store_terminal_receipt(
                    candidate=candidate,
                    intent=intent,
                    receipt=receipt,
                    expected_status="CONTEXT_READY",
                    phase_claim=claim,
                    observation={
                        "publication_receipt": {"review_id": 999}
                    },
                    table=self.table,
                )
            self.assertEqual(
                persistence.get_item(
                    "owner/repo", 7, table=self.table
                )["status"],
                "CONTEXT_READY",
            )
            self.assertTrue(
                store_terminal_receipt(
                    candidate=candidate,
                    intent=intent,
                    receipt=receipt,
                    expected_status="CONTEXT_READY",
                    phase_claim=claim,
                    table=self.table,
                )
            )
            self.assertTrue(
                store_terminal_receipt(
                    candidate=candidate,
                    intent=intent,
                    receipt=receipt,
                    expected_status="CONTEXT_READY",
                    phase_claim=claim,
                    table=self.table,
                )
            )
            conflicting = deepcopy(receipt)
            object.__setattr__(conflicting, "review_id", 999)
            with self.assertRaises(PublicationStateConflict):
                store_terminal_receipt(
                    candidate=candidate,
                    intent=intent,
                    receipt=conflicting,
                    expected_status="CONTEXT_READY",
                    phase_claim=claim,
                    table=self.table,
                )

    def test_takeover_after_dispatch_fences_old_receipt_then_adopts_once(self):
        self._put_item()
        first_claim = self._claim()
        candidate = _candidate(inline=True)
        with patch.object(
            persistence, "get_s3_client", return_value=self.s3
        ):
            intent = persist_prepared_intent(
                candidate,
                expected_status="CONTEXT_READY",
                phase_claim=first_claim,
                table=self.table,
            )
            intent = mark_dispatching(
                repo="owner/repo",
                pr_number=7,
                expected_status="CONTEXT_READY",
                intent=intent,
                phase_claim=first_claim,
                table=self.table,
            )
            old_receipt = execute_dispatching(
                self.repo,
                7,
                intent=intent,
                candidate=candidate,
            )
            second_claim = self._claim(request="request-2")
            with self.assertRaises(PublicationStateConflict):
                store_terminal_receipt(
                    candidate=candidate,
                    intent=intent,
                    receipt=old_receipt,
                    expected_status="CONTEXT_READY",
                    phase_claim=first_claim,
                    table=self.table,
                )
            current = persistence.get_item(
                "owner/repo", 7, table=self.table, consistent_read=True
            )
            self.assertEqual(current["status"], "CONTEXT_READY")
            self.assertEqual(
                current["publication_intent"]["state"], "dispatching"
            )
            self.assertTrue(
                recover_publication_transaction(
                    current_item=current,
                    expected_status="CONTEXT_READY",
                    phase_claim=second_claim,
                    recovery_runtime_identity={
                        "phase": "review",
                        "aws_request_id": "request-2",
                    },
                    repository_for=lambda _repo: self.repo,
                    pre_publish_check_for=lambda _candidate: (
                        lambda: self.fail(
                            "dispatching recovery must not preflight"
                        )
                    ),
                    table=self.table,
                )
            )
        terminal = persistence.get_item(
            "owner/repo", 7, table=self.table
        )
        self.assertEqual(self.pull.create_count, 1)
        self.assertEqual(terminal["status"], "PROCESSED")
        self.assertEqual(
            terminal["publication_receipt"]["outcome"], "adopted"
        )
        self.assertEqual(terminal["publication_recovery_attempt"], 2)

    def test_foreign_event_cannot_take_over_active_claim(self):
        self._put_item()
        first = self._claim()
        foreign = persistence.claim_phase_attempt(
            "owner/repo",
            7,
            "review",
            expected_status="CONTEXT_READY",
            runtime_identity={"aws_request_id": "foreign"},
            stream_event_id="foreign-event",
            table=self.table,
        )
        self.assertEqual(first["attempt"], 1)
        self.assertIsNone(foreign)

    def test_same_event_retry_after_pre_intent_crash_advances_attempt(self):
        for phase in ("context", "review"):
            with self.subTest(phase=phase):
                self.table.reset()
                self._put_item(phase=phase)
                first = self._claim(phase)
                duplicate = self._claim(phase, request="request-2")
                current = persistence.get_item(
                    "owner/repo", 7, table=self.table
                )
                self.assertEqual(first["attempt"], 1)
                self.assertEqual(duplicate["attempt"], 2)
                self.assertEqual(current[f"{phase}_attempt"], 2)
                self.assertEqual(
                    current[f"{phase}_claim"]["owner_id"],
                    "request-2",
                )

    def test_stale_owner_cannot_mutate_intent_after_same_event_takeover(self):
        self._put_item()
        first = self._claim()
        candidate = _candidate()
        with patch.object(
            persistence, "get_s3_client", return_value=self.s3
        ):
            intent = persist_prepared_intent(
                candidate,
                expected_status="CONTEXT_READY",
                phase_claim=first,
                table=self.table,
            )
            second = self._claim(request="request-2")
            self.assertEqual(second["attempt"], 2)
            self.assertFalse(
                persistence.replace_publication_intent(
                    "owner/repo",
                    7,
                    expected_status="CONTEXT_READY",
                    expected_intent=intent,
                    next_intent={**intent, "publication_recovery_attempt": 2},
                    phase_claim=first,
                    table=self.table,
                )
            )


if __name__ == "__main__":
    unittest.main()
