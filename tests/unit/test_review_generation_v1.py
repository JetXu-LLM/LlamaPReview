import copy
import json
import unittest
from unittest.mock import patch

from tests.unit.fakes import (
    ensure_repo_root_on_path,
    install_fake_requests_module,
    set_default_env,
)

ensure_repo_root_on_path()
set_default_env()
install_fake_requests_module()

from lambdas.LlamaPReviewPipeline.review import generate as generation
from lambdas.LlamaPReviewPipeline.deepseek_client import (
    DeepSeekClient,
    ProviderCallLedgerError,
)
from lambdas.LlamaPReviewPipeline.review.presentation import (
    PresentationIssue,
    PresentationResult,
)


class _Client:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def chat(self, messages, **kwargs):
        self.calls.append(
            {
                "messages": [dict(message) for message in messages],
                **kwargs,
            }
        )
        if not self.responses:
            raise AssertionError("unexpected extra model call")
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def _response(content, *, finish_reason="stop", token_count=1):
    return {
        "choices": [
            {
                "message": {"role": "assistant", "content": content},
                "finish_reason": finish_reason,
            }
        ],
        "usage": {
            "prompt_tokens": token_count,
            "completion_tokens": token_count,
            "total_tokens": token_count * 2,
        },
    }


class _HTTPResponse:
    status_code = 200
    headers = {}

    def __init__(self, content="paid Deep response"):
        self.content = content

    def json(self):
        return _response(self.content, token_count=2)


def _publishable(*, safe_partial=False):
    presentation = {
        "version": "presentation_v1",
        "decision": {
            "verdict": "clear",
            "confidence": "High",
            "summary": "The exact-head change is ready.",
            "owner_actions": [],
        },
        "findings": [],
        "material_unknowns": [],
        "confidence_checks": [],
        "diagram": None,
    }
    return PresentationResult(
        status="publishable",
        review={
            "pr_review_comment": "### LlamaPReview — Clear\n\nReady.",
            "inline_comments": [],
            "v3_review": {"schema_version": 3},
            "presentation_v1": presentation,
            "review_quality_warnings": [],
            "visible_projection_source": "presentation_v1",
        },
        presentation=presentation,
        normalizations=("optional_surface_removed",) if safe_partial else (),
        safe_partial=safe_partial,
    )


def _presentation_failure(kind="json_parse_error", *, verdict="clear"):
    presentation = None
    if verdict is not None:
        presentation = copy.deepcopy(_publishable().presentation)
        presentation["decision"]["verdict"] = verdict
        presentation["representation_noise"] = True
    return PresentationResult(
        status="failure",
        review=None,
        presentation=presentation,
        issues=(
            PresentationIssue(
                kind,
                "$",
                "private representation diagnostic",
                "representation",
            ),
        ),
        failure_kind=kind,
    )


def _failure(kind="out_of_catalog_material_evidence"):
    return PresentationResult(
        status="failure",
        review=None,
        presentation=None,
        issues=(
            PresentationIssue(
                kind,
                "$",
                "private truth diagnostic",
                "truth",
            ),
        ),
        failure_kind=kind,
    )


REAL_PR_DETAILS = """# Pull Request #17

## File Changes
### src/app.py
```diff
@@ -1 +1 @@
-value = build(1)
+value = build(2)
```
"""


def _real_presentation(*, supporting_ci: bool) -> dict:
    return {
        "version": "presentation_v1",
        "decision": {
            "verdict": "clear",
            "confidence": "High",
            "summary": (
                "No review blocker found. The changed expression remains "
                "consistent with the visible contract."
            ),
            "owner_actions": [],
        },
        "findings": [
            {
                "headline": "The changed value remains locally consistent",
                "priority": "P2",
                "category": "note",
                "confidence": "High",
                "file_path": "src/app.py",
                "code_snippet": "value = build(2)",
                "analysis": (
                    "The changed expression preserves the local call shape."
                ),
                "owner_action": "Keep the focused regression coverage.",
                "required_evidence_refs": ["path:src/app.py"],
                "supporting_evidence_refs": (
                    ["ci:unit"] if supporting_ci else []
                ),
                "placement": "inline",
                "suggestion": None,
            }
        ],
        "material_unknowns": [],
        "confidence_checks": [
            {
                "check": "Exact-head unit result",
                "result": "The recorded check completed successfully.",
                "evidence_refs": ["ci:unit"],
            }
        ],
        "diagram": None,
    }


def _real_context_meta() -> dict:
    return {
        "head_sha": "a" * 40,
        "analyzer_result": {"pr_type": "code", "risk_domains": []},
        "ci_generation_model_payload": {
            "checks": [
                {
                    "identity": "unit",
                    "name": "Unit check",
                    "status": "completed",
                    "classification": "success",
                    "conclusion": "success",
                }
            ]
        },
        "evidence_catalog": [
            {
                "id": "path:src/app.py",
                "source_type": "diff",
                "outcome": "hit",
                "paths": ["src/app.py"],
                "coverage_type": "changed_region",
            },
            {
                "id": "ci:unit",
                "source_type": "ci",
                "outcome": "success",
                "paths": ["src/app.py"],
                "coverage_type": "changed_region",
            },
        ],
    }


class ReviewGenerationV1Tests(unittest.TestCase):
    def test_deep_ledger_failure_escapes_generation_without_second_call(self):
        client = DeepSeekClient(api_key="key")

        def reject(_record):
            raise RuntimeError("durable ledger unavailable")

        client.set_provider_call_sink(reject)
        with patch(
            "lambdas.LlamaPReviewPipeline.deepseek_client.requests.post",
            return_value=_HTTPResponse(),
        ) as post:
            with self.assertRaises(ProviderCallLedgerError) as raised:
                generation.generate_review(
                    "PR intent",
                    "## PFR Review Context\nExact-head evidence",
                    client=client,
                    context_meta={},
                    model="deepseek-v4-pro",
                    reasoning_effort="max",
                )

        self.assertEqual(post.call_count, 1)
        self.assertEqual(
            raised.exception.provider_call_record["phase"],
            "deep_judgment",
        )
        records = client.provider_call_records()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["status"], "completed")
        self.assertEqual(records[0]["logical_model"], "deepseek-v4-pro")
        self.assertEqual(records[0]["billed_model"], "deepseek-v4-flash")
        self.assertEqual(records[0]["usage_state"], "reported")

    def test_final_ledger_failure_escapes_generation_without_repair(self):
        client = DeepSeekClient(api_key="key")
        persisted = []

        def reject_second(record):
            persisted.append(record)
            if len(persisted) == 2:
                raise RuntimeError("durable ledger unavailable")

        client.set_provider_call_sink(reject_second)
        with patch(
            "lambdas.LlamaPReviewPipeline.deepseek_client.requests.post",
            side_effect=[
                _HTTPResponse("complete staff-level memo"),
                _HTTPResponse('{"version":"presentation_v1"}'),
            ],
        ) as post, patch.object(
            generation,
            "compile_presentation_v1",
        ) as compiler:
            with self.assertRaises(ProviderCallLedgerError) as raised:
                generation.generate_review(
                    "PR intent",
                    "## PFR Review Context\nExact-head evidence",
                    client=client,
                    context_meta={},
                    model="deepseek-v4-pro",
                    reasoning_effort="max",
                )

        self.assertEqual(post.call_count, 2)
        compiler.assert_not_called()
        self.assertEqual(
            raised.exception.provider_call_record["phase"],
            "final_presentation",
        )
        self.assertEqual(len(client.provider_call_records()), 2)

    def test_representation_failure_stops_after_final_without_extra_call(self):
        client = DeepSeekClient(api_key="key")
        persisted = []

        def persist(record):
            persisted.append(record)

        client.set_provider_call_sink(persist)
        with patch(
            "lambdas.LlamaPReviewPipeline.deepseek_client.requests.post",
            side_effect=[
                _HTTPResponse("complete staff-level memo"),
                _HTTPResponse('{"version":"presentation_v1"}'),
                _HTTPResponse('{"version":"presentation_v1"}'),
            ],
        ) as post, patch.object(
            generation,
            "compile_presentation_v1",
            return_value=_presentation_failure(),
        ) as compiler:
            result = generation.generate_review(
                "PR intent",
                "## PFR Review Context\nExact-head evidence",
                client=client,
                context_meta={},
                model="deepseek-v4-pro",
                reasoning_effort="max",
            )

        self.assertEqual(post.call_count, 2)
        compiler.assert_called_once()
        self.assertEqual(len(client.provider_call_records()), 2)
        self.assertFalse(result["review_publishable"])
        self.assertNotIn("review_presentation_repair_attempted", result)
        self.assertEqual(result["review_failure_kind"], "json_parse_error")

    def test_ordinary_path_is_one_free_deep_and_one_json_final(self):
        evidence_sentinel = "RAW_DIFF_AND_PFR_SENTINEL_8193"
        changed_delta_sentinel = "CHANGED_DELTA_FOR_VISUAL_2381"
        client = _Client(
            [
                _response("complete staff-level memo", token_count=2),
                _response('{"version":"presentation_v1"}', token_count=3),
            ]
        )
        phase_sink = []
        with patch.object(
            generation,
            "compile_presentation_v1",
            return_value=_publishable(),
        ):
            result = generation.generate_review(
                "PR intent",
                f"## PFR Review Context\n{evidence_sentinel}",
                client=client,
                context_meta={
                    "changed_delta_focus": {
                        "files": [
                            {
                                "path": "src/flow.py",
                                "change_type": "modified",
                                "patch": changed_delta_sentinel,
                                "diff_coverage": "complete",
                            }
                        ],
                        "packing": {},
                    }
                },
                model="deepseek-v4-pro",
                reasoning_effort="max",
                phase_sink=phase_sink,
            )

        self.assertTrue(result["review_publishable"])
        self.assertTrue(result["review_publication_safe"])
        self.assertNotIn("review_presentation_repair_attempted", result)
        self.assertEqual(
            result["review_presentation_selected_phase"],
            "final_presentation",
        )
        self.assertEqual(result["review_model_finish_reason"], "stop")
        self.assertFalse(result["review_final_thinking"])
        self.assertEqual(result["review_final_reasoning_effort"], "")
        self.assertEqual(
            [phase["phase"] for phase in result["review_model_phases"]],
            ["deep_judgment", "final_presentation"],
        )
        self.assertEqual(result["deepseek_usage_total"]["total_tokens"], 10)
        self.assertEqual(phase_sink, result["review_model_phases"])

        deep_call, final_call = client.calls
        self.assertTrue(deep_call["thinking"])
        self.assertEqual(deep_call["reasoning_effort"], "max")
        self.assertNotIn("response_format", deep_call)
        self.assertNotIn("tools", deep_call)
        self.assertEqual(
            deep_call["max_tokens"],
            generation.config.REVIEW_DEEP_THINKING_MAX_TOKENS,
        )

        self.assertFalse(final_call["thinking"])
        self.assertEqual(final_call["reasoning_effort"], "")
        self.assertEqual(
            final_call["response_format"],
            {"type": "json_object"},
        )
        self.assertNotIn("tools", final_call)
        self.assertIn(
            evidence_sentinel,
            deep_call["messages"][1]["content"],
        )
        self.assertNotIn(
            evidence_sentinel,
            "\n".join(
                str(message.get("content") or "")
                for message in final_call["messages"]
            ),
        )
        self.assertIn(
            changed_delta_sentinel,
            final_call["messages"][3]["content"],
        )
        self.assertEqual(
            final_call["messages"][2],
            {"role": "assistant", "content": "complete staff-level memo"},
        )
        self.assertIn(
            "presentation object",
            final_call["messages"][3]["content"],
        )

    def test_representation_failure_never_spends_a_third_model_call(self):
        failed_presentation = copy.deepcopy(_publishable().presentation)
        failed_presentation["representation_noise"] = True
        failed_final = json.dumps(failed_presentation)
        evidence_sentinel = "REPAIR_RAW_CONTEXT_SENTINEL_4937"
        client = _Client(
            [
                _response("Deep memo"),
                _response(failed_final),
            ]
        )
        with patch.object(
            generation,
            "compile_presentation_v1",
            return_value=_presentation_failure(),
        ):
            result = generation.generate_review(
                "PR intent",
                f"## PFR Review Context\n{evidence_sentinel}",
                client=client,
                context_meta={},
            )

        self.assertFalse(result["review_publishable"])
        self.assertNotIn("review_presentation_repair_attempted", result)
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(
            [phase["phase"] for phase in result["review_model_phases"]],
            ["deep_judgment", "final_presentation"],
        )

    def test_misplaced_supporting_ci_is_removed_without_repair(self):
        final = json.dumps(_real_presentation(supporting_ci=True))
        client = _Client(
            [
                _response("Deep memo"),
                _response(final),
            ]
        )

        result = generation.generate_review(
            REAL_PR_DETAILS,
            "Exact-head context",
            client=client,
            context_meta=_real_context_meta(),
        )

        self.assertTrue(result["review_publishable"])
        self.assertNotIn("review_presentation_repair_attempted", result)
        self.assertNotIn("review_presentation_repair_recovered", result)
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(
            result["presentation_v1"]["findings"][0][
                "supporting_evidence_refs"
            ],
            [],
        )
        self.assertEqual(
            result["presentation_v1"]["confidence_checks"][0][
                "evidence_refs"
            ],
            ["ci:unit"],
        )

    def test_truncated_final_with_visible_content_fails_without_repair(self):
        truncated = '{"version":"presentation_v1"}'
        client = _Client(
            [
                _response("Deep memo"),
                _response(truncated, finish_reason="length", token_count=4),
            ]
        )
        with patch.object(
            generation,
            "compile_presentation_v1",
            return_value=_publishable(),
        ) as compiler:
            result = generation.generate_review(
                "PR intent",
                "context",
                client=client,
                context_meta={},
            )

        self.assertFalse(result["review_publishable"])
        self.assertNotIn("review_presentation_repair_attempted", result)
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(compiler.call_count, 1)
        self.assertEqual(result["review_model_finish_reason"], "length")
        self.assertEqual(
            result["review_stage_finish_reasons"]["final_presentation"],
            "length",
        )
        self.assertEqual(
            result["review_model_phases"][1]["usage"]["total_tokens"],
            8,
        )

    def test_safe_local_partial_publishes_without_model_repair(self):
        client = _Client(
            [
                _response("Deep memo"),
                _response('{"version":"presentation_v1"}'),
            ]
        )
        with patch.object(
            generation,
            "compile_presentation_v1",
            return_value=_publishable(safe_partial=True),
        ):
            result = generation.generate_review(
                "PR intent",
                "context",
                client=client,
                context_meta={},
            )

        self.assertEqual(len(client.calls), 2)
        self.assertTrue(result["review_presentation_safe_partial"])
        self.assertNotIn("review_presentation_repair_attempted", result)
        self.assertIn(
            "presentation_safe_partial",
            result["review_quality_warnings"],
        )

    def test_failed_final_is_typed_and_never_leaks_model_content(self):
        raw_deep = "private Deep wording"
        raw_final = "{broken Final"
        client = _Client(
            [
                _response(raw_deep),
                _response(raw_final),
            ]
        )
        with patch.object(
            generation,
            "compile_presentation_v1",
            return_value=_presentation_failure("json_root_invalid"),
        ):
            result = generation.generate_review(
                "PR intent",
                "context",
                client=client,
                context_meta={},
            )

        self.assertEqual(len(client.calls), 2)
        self.assertFalse(result["review_publishable"])
        self.assertFalse(result["review_publication_safe"])
        self.assertNotIn("review_presentation_repair_attempted", result)
        self.assertNotIn("review_presentation_repair_recovered", result)
        self.assertNotIn("pr_review_comment", result)
        self.assertNotIn("inline_comments", result)
        serialized = json.dumps(result, sort_keys=True)
        self.assertNotIn(raw_deep, serialized)
        self.assertNotIn(raw_final, serialized)
        self.assertEqual(
            result["review_failure_kind"],
            "json_root_invalid",
        )

    def test_failed_blocking_final_cannot_be_rewritten_by_a_third_call(self):
        client = _Client(
            [
                _response("Deep blocking memo"),
                _response('{"version":"presentation_v1"}'),
            ]
        )
        with patch.object(
            generation,
            "compile_presentation_v1",
            return_value=_presentation_failure(
                "deciding_item_loss",
                verdict="blocking",
            ),
        ):
            result = generation.generate_review(
                "PR intent",
                "context",
                client=client,
                context_meta={},
            )

        self.assertEqual(len(client.calls), 2)
        self.assertFalse(result["review_publishable"])
        self.assertEqual(
            result["review_failure_kind"],
            "deciding_item_loss",
        )
        self.assertNotIn("review_presentation_repair_attempted", result)
        self.assertNotIn("pr_review_comment", result)

    def test_incomplete_final_fails_closed_without_a_third_call(self):
        initial_visible = '{"version":"presentation_v1"}'
        client = _Client(
            [
                _response("Deep clear memo"),
                _response(initial_visible, finish_reason="length"),
            ]
        )
        with patch.object(
            generation,
            "compile_presentation_v1",
            return_value=_publishable(),
        ):
            result = generation.generate_review(
                "PR intent",
                "context",
                client=client,
                context_meta={},
            )

        self.assertEqual(len(client.calls), 2)
        self.assertFalse(result["review_publishable"])
        self.assertFalse(result["review_presentation_safe_partial"])
        self.assertNotIn("review_presentation_repair_attempted", result)
        self.assertNotIn("review_presentation_repair_recovered", result)
        self.assertNotIn("review_presentation_selected_phase", result)
        self.assertEqual(result["review_model_finish_reason"], "length")
        self.assertFalse(result["review_final_thinking"])
        self.assertEqual(result["review_final_reasoning_effort"], "")
        self.assertFalse(result["quality_scoreable"])
        self.assertEqual(
            result["review_failure_kind"],
            "incomplete_provider_envelope",
        )
        self.assertEqual(
            result["review_stage_finish_reasons"]["final_presentation"],
            "length",
        )

    def test_truth_failure_does_not_spend_representation_repair(self):
        client = _Client(
            [
                _response("Deep memo"),
                _response('{"version":"presentation_v1"}'),
            ]
        )
        with patch.object(
            generation,
            "compile_presentation_v1",
            return_value=_failure(),
        ):
            result = generation.generate_review(
                "PR intent",
                "context",
                client=client,
                context_meta={},
            )

        self.assertEqual(len(client.calls), 2)
        self.assertFalse(result["review_publishable"])
        self.assertNotIn("review_presentation_repair_attempted", result)
        self.assertEqual(
            result["review_failure_kind"],
            "out_of_catalog_material_evidence",
        )


if __name__ == "__main__":
    unittest.main()
