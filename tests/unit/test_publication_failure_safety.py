import unittest
from unittest.mock import patch

from tests.unit.fakes import ensure_repo_root_on_path, set_default_env

ensure_repo_root_on_path()
set_default_env()

from lambdas.LlamaPReviewPipeline.review.publish import (
    PUBLIC_FOOTER,
    _RETIRED_PUBLIC_FOOTER_VARIANTS,
    PUBLIC_FOOTER_VARIANTS,
    public_footer,
    build_diff_maps_from_pr_files,
    build_main_comment,
    prepare_main_comment_publication,
    prepare_review_publication,
)
from lambdas.LlamaPReviewPipeline.review.publication_candidate import (
    build_candidate,
    prepared_from_candidate,
)


HEAD = "a" * 40


def _publishable_review(*, body: str = "### Review\n\nNo blocker found.", inline=None):
    return {
        "review_generation_status": "complete",
        "review_publishable": True,
        "review_publication_safe": True,
        "review_fallback_used": False,
        "pr_review_comment": body,
        "inline_comments": list(inline or []),
    }


def _inline(*, path: str = "src/app.py"):
    return {
        "file_path": path,
        "code_snippet": "value = 2",
        "comment": "Keep the changed value within the validated boundary.",
        "priority": "P1",
        "confidence": "High",
    }


def _diff_maps():
    return build_diff_maps_from_pr_files(
        [
            {
                "filename": "src/app.py",
                "patch": "@@ -1 +1 @@\n-value = 1\n+value = 2\n",
            }
        ]
    )


class PublicationFailureSafetyTests(unittest.TestCase):
    def test_nonpublishable_failure_has_no_synthetic_clear_comment(self):
        body = build_main_comment(
            {
                "review_generation_status": "incomplete",
                "review_publishable": False,
                "review_failure_kind": "presentation_validation_error",
            }
        )
        self.assertEqual(body, "")
        self.assertFalse(any(variant in body for variant in PUBLIC_FOOTER_VARIANTS))

    def test_complete_review_requires_model_derived_comment(self):
        with self.assertRaisesRegex(
            ValueError,
            "non-empty model-derived comment",
        ):
            build_main_comment(
                {
                    "review_generation_status": "complete",
                    "review_publishable": True,
                }
            )

    def test_substantive_main_review_gets_one_code_owned_footer(self):
        body = build_main_comment(
            _publishable_review(
                body="### LlamaPReview — Ready to merge\n\nNo blocker found."
            )
        )
        self.assertEqual(sum(body.count(variant) for variant in PUBLIC_FOOTER_VARIANTS), 1)
        self.assertEqual(body.count(PUBLIC_FOOTER), 1)

    def test_model_marker_phrase_cannot_suppress_the_exact_footer(self):
        body = build_main_comment(
            _publishable_review(
                body=(
                    "### Review\n\nA source note uses the phrase "
                    "LlamaPReview is open source. without the code-owned block."
                )
            )
        )

        self.assertEqual(body.count(PUBLIC_FOOTER), 1)
        self.assertTrue(body.endswith(PUBLIC_FOOTER))

    def test_nonpublishable_body_never_gets_footer(self):
        body = build_main_comment(
            {
                **_publishable_review(body="Review failed safely."),
                "review_generation_status": "incomplete",
                "review_publishable": False,
            }
        )

        self.assertEqual(body, "Review failed safely.")
        self.assertNotIn(PUBLIC_FOOTER, body)

    def test_non_model_terminal_body_never_gets_footer(self):
        prepared = prepare_main_comment_publication(
            "Review skipped: no substantive review target.",
            head_sha=HEAD,
            review_mode="skip",
        )

        self.assertNotIn(PUBLIC_FOOTER, prepared.main_body)

    def test_local_inline_degradation_retains_one_main_footer(self):
        prepared = prepare_review_publication(
            _publishable_review(inline=[_inline(path="src/missing.py")]),
            head_sha=HEAD,
            diff_maps=_diff_maps(),
        )

        self.assertEqual(prepared.comments, ())
        self.assertIn("Unanchored Suggestions", prepared.main_body)
        self.assertEqual(prepared.main_body.count(public_footer(HEAD)), 1)
        self.assertTrue(prepared.main_body.endswith(public_footer(HEAD)))

    def test_inline_body_never_contains_footer(self):
        prepared = prepare_review_publication(
            _publishable_review(inline=[_inline()]),
            head_sha=HEAD,
            diff_maps=_diff_maps(),
        )

        self.assertEqual(len(prepared.comments), 1)
        self.assertEqual(prepared.main_body.count(public_footer(HEAD)), 1)
        self.assertTrue(
            all(
                not any(variant in comment["body"] for variant in PUBLIC_FOOTER_VARIANTS)
                for comment in prepared.comments
            )
        )

    def test_recovery_round_trip_cannot_duplicate_footer(self):
        prepared = prepare_review_publication(
            _publishable_review(),
            head_sha=HEAD,
            diff_maps={},
        )
        candidate = build_candidate(
            prepared,
            repo="owner/repo",
            pr_number=7,
            run_id="run-7",
            phase="review",
            owner_event_id="stream-event-7",
            owner_request_id="request-7",
            publication_generation_attempt=1,
            preflight_completed_at="2026-08-09T00:00:00+00:00",
            generation_runtime_identity={"request_id": "request-7"},
            terminal_attributes={},
            publication_key="f" * 32,
        )

        recovered = prepared_from_candidate(candidate)
        rebuilt = build_main_comment(
            _publishable_review(body=recovered.main_body),
            invitation_seed=HEAD,
        )

        self.assertEqual(recovered.main_body, prepared.main_body)
        self.assertEqual(rebuilt, prepared.main_body)
        self.assertEqual(rebuilt.count(public_footer(HEAD)), 1)

    def test_rebuilding_under_a_different_head_still_leaves_one_footer(self):
        prepared = prepare_review_publication(
            _publishable_review(),
            head_sha=HEAD,
            diff_maps={},
        )
        other = "0" * 40
        self.assertNotEqual(public_footer(HEAD), public_footer(other))

        rebuilt = build_main_comment(
            _publishable_review(body=prepared.main_body),
            invitation_seed=other,
        )

        self.assertEqual(sum(rebuilt.count(variant) for variant in PUBLIC_FOOTER_VARIANTS), 1)
        self.assertTrue(rebuilt.endswith(public_footer(other)))
        self.assertNotIn(public_footer(HEAD), rebuilt)

    def test_footer_variants_match_the_two_approved_invitations(self):
        self.assertEqual(
            PUBLIC_FOOTER_VARIANTS,
            (
                "\n\n---\n*LlamaPReview is open source. "
                "[Explore the code behind this review.](https://github.com/JetXu-LLM/LlamaPReview)*",
                "\n\n---\n*Still coding alone? "
                "[Open CarbonChat, a chat room right inside Codex.](https://carbonchat.codexforwork.com/)*",
            ),
        )
        self.assertEqual(public_footer(HEAD), PUBLIC_FOOTER_VARIANTS[0])
        self.assertEqual(public_footer("0" * 40), PUBLIC_FOOTER_VARIANTS[1])

    def test_every_footer_is_one_line_with_one_untracked_link(self):
        for variant in PUBLIC_FOOTER_VARIANTS:
            with self.subTest(variant=variant):
                self.assertTrue(variant.startswith("\n\n---\n*"))
                self.assertTrue(variant.endswith(")*"))
                self.assertEqual(variant.strip().count("\n"), 1)
                self.assertEqual(variant.count("](https://"), 1)
                self.assertNotIn("utm_", variant)

    def test_rebuilding_retired_or_current_footers_leaves_one_invitation(self):
        model_body = "### Review\n\nKeep the supported finding."
        for seed in (HEAD, "0" * 40):
            for old_footer in _RETIRED_PUBLIC_FOOTER_VARIANTS + PUBLIC_FOOTER_VARIANTS:
                with self.subTest(seed=seed, old_footer=old_footer):
                    rebuilt = build_main_comment(
                        _publishable_review(body=model_body + old_footer),
                        invitation_seed=seed,
                    )
                    self.assertEqual(rebuilt, model_body + public_footer(seed))
                    self.assertFalse(any(
                        variant in rebuilt for variant in _RETIRED_PUBLIC_FOOTER_VARIANTS
                    ))

    def test_saved_candidate_recovery_preserves_retired_and_current_bytes(self):
        for footer in _RETIRED_PUBLIC_FOOTER_VARIANTS + PUBLIC_FOOTER_VARIANTS:
            with self.subTest(footer=footer):
                prepared = prepare_main_comment_publication(
                    "### Review\n\nSaved judgment." + footer,
                    head_sha=HEAD,
                    review_mode="normal",
                )
                candidate = build_candidate(
                    prepared,
                    repo="owner/repo",
                    pr_number=7,
                    run_id="run-7",
                    phase="review",
                    owner_event_id="stream-event-7",
                    owner_request_id="request-7",
                    publication_generation_attempt=1,
                    preflight_completed_at="2026-10-08T00:00:00+00:00",
                    generation_runtime_identity={"request_id": "request-7"},
                    terminal_attributes={},
                    publication_key="f" * 32,
                )
                with patch(
                    "lambdas.LlamaPReviewPipeline.review.publish.public_footer",
                    side_effect=AssertionError("recovery must not choose an invitation"),
                ):
                    recovered = prepared_from_candidate(candidate)
                self.assertEqual(recovered.request_payload(), prepared.request_payload())
                self.assertEqual(recovered.payload_sha256, prepared.payload_sha256)

    def test_footer_choice_is_stable_and_spreads_across_heads(self):
        self.assertEqual(public_footer(HEAD), public_footer(HEAD))
        chosen = {
            public_footer(f"{index:040x}") for index in range(200)
        }
        self.assertEqual(chosen, set(PUBLIC_FOOTER_VARIANTS))

    def test_exact_head_payload_changes_only_by_code_owned_footer(self):
        model_body = "### Review\n\nThe exact-head evidence is internally consistent."
        prepared = prepare_review_publication(
            _publishable_review(body=model_body, inline=[_inline()]),
            head_sha=HEAD,
            diff_maps=_diff_maps(),
        )
        payload = prepared.request_payload()
        without_footer = {**payload, "body": model_body}

        self.assertEqual(payload["head_sha"], without_footer["head_sha"])
        self.assertEqual(payload["event"], without_footer["event"])
        self.assertEqual(payload["comments"], without_footer["comments"])
        self.assertEqual(payload["body"], model_body + public_footer(HEAD))
        self.assertNotEqual(prepared.payload_sha256, "")

    def test_main_comment_ignores_retired_diagram_fields(self):
        body = build_main_comment(
            {
                **_publishable_review(
                    body="### Review\n\nCurrent presentation."
                ),
                "documentation_diagram": (
                    "```mermaid\nsequenceDiagram\nA->>B: old\n```"
                ),
                "risk_diagram": (
                    "```mermaid\nsequenceDiagram\nA->>B: old risk\n```"
                ),
            }
        )

        self.assertNotIn("Documentation Diagram", body)
        self.assertNotIn("Risk Diagram", body)
        self.assertNotIn("sequenceDiagram", body)


if __name__ == "__main__":
    unittest.main()
