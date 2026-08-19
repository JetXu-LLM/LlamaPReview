import unittest

from lambdas.LlamaPReviewPipeline.review.language_fences import (
    language_fence_for_path,
)
from lambdas.LlamaPReviewPipeline.review.rendering_safety import (
    ALLOWED_ARROWS_SUPERSET,
    format_mermaid,
    validate_sequence_mermaid_offline,
)
from lambdas.LlamaPReviewPipeline.review.terminal_messages import (
    skipped_review_notice,
)


class ReviewSupportCapabilityTests(unittest.TestCase):
    def test_language_fence_is_path_owned_and_conservative(self):
        self.assertEqual(language_fence_for_path("Dockerfile"), "dockerfile")
        self.assertEqual(
            language_fence_for_path("build.gradle.kts"),
            "kotlin",
        )
        self.assertEqual(language_fence_for_path("NOTICE"), "unknown")

    def test_terminal_skip_notice_does_not_imply_model_judgment(self):
        skipped = skipped_review_notice("No substantive review target.")

        self.assertIn("Review skipped", skipped)
        self.assertIn("No model-driven code review was run", skipped)

    def test_mermaid_renderer_accepts_valid_sequence_and_rejects_noise(self):
        valid = format_mermaid(
            "sequenceDiagram\nparticipant A\nparticipant B\nA->>B: call"
        )
        invalid = format_mermaid("graph TD\nA-->B")

        self.assertTrue(valid.startswith("```mermaid\nsequenceDiagram"))
        self.assertEqual(invalid, "")

    def test_mermaid_renderer_normalizes_supported_common_syntax_patterns(self):
        rendered = format_mermaid(
            "```mermaid\n"
            "sequenceDiagram\n"
            "participant A as API #1\n"
            "participant B as Broker\n"
            "note over A,B\n"
            "PR change; validate # route\n"
            "end note\n"
            "A->>B: dispatch; job #1\n"
            "```"
        )

        self.assertTrue(rendered.startswith("```mermaid\nsequenceDiagram"))
        self.assertIn(
            "note over A,B: PR change#59; validate #35; route",
            rendered,
        )
        self.assertIn("A->>B: dispatch#59; job #35;1", rendered)
        self.assertNotIn("end note", rendered)

    def test_mermaid_renderer_repairs_opt_with_else_as_alt(self):
        rendered = format_mermaid(
            "sequenceDiagram\n"
            "participant C as Client\n"
            "participant A as API\n"
            "opt Accepted\n"
            "C->>A: Submit\n"
            "else Rejected\n"
            "A-->>C: Error\n"
            "end"
        )

        self.assertIn("\nalt Accepted\n", rendered)
        self.assertNotIn("\nopt Accepted\n", rendered)
        self.assertIn("\nelse Rejected\n", rendered)

    def test_mermaid_renderer_repairs_standalone_impact_as_a_note(self):
        rendered = format_mermaid(
            "sequenceDiagram\n"
            "participant C as Client\n"
            "participant W as Worker\n"
            "C->>W: Submit\n"
            "Impact — failed work remains queued"
        )

        self.assertIn(
            "note over C,W: Impact — failed work remains queued",
            rendered,
        )

    def test_bare_impact_without_participants_is_not_guessed(self):
        rendered = format_mermaid(
            "sequenceDiagram\n"
            "A->>B: Submit\n"
            "Impact — failed work remains queued"
        )

        self.assertEqual(rendered, "")

    def test_mermaid_renderer_closes_one_unambiguous_eof_block(self):
        rendered = format_mermaid(
            "sequenceDiagram\n"
            "participant C as Client\n"
            "participant W as Worker\n"
            "critical Unsafe changed path\n"
            "C->>W: Dispatch before validation"
        )

        self.assertTrue(rendered.endswith("\nend\n```"))
        self.assertEqual(rendered.count("\nend\n"), 1)

    def test_mermaid_renderer_does_not_repair_multiple_defects(self):
        rendered = format_mermaid(
            "sequenceDiagram\n"
            "participant C as Client\n"
            "critical Unsafe changed path\n"
            "this is not Mermaid"
        )

        self.assertEqual(rendered, "")


class MermaidArrowLookalikeTests(unittest.TestCase):
    """Prose must never be admitted as a message by arrow-lookalike characters.

    A published review once carried a diagram GitHub could not render because a
    wrapped note line was parsed as ``e`` -x-> ``tract``.
    """

    WRAPPED_NOTE_DIAGRAM = (
        "sequenceDiagram\n"
        "participant G as Git\n"
        "participant Ext as Extractor script\n"
        "participant GL as gitleaks dir\n"
        "G->>Ext: merge delta blobs (all paths)\n"
        "Ext->>Ext: rename suppression paths\n"
        "Note over Ext: PR change - non-injective rename\n"
        "extract both to same OUT path\n"
        "Ext->>G: git show m:p1 > OUT\n"
        "GL-->>Ext: clean (blob p1 never scanned)"
    )

    def test_wrapped_note_remainder_is_folded_back_into_its_note(self):
        rendered = format_mermaid(self.WRAPPED_NOTE_DIAGRAM)

        self.assertIn(
            "Note over Ext: PR change - non-injective rename"
            "<br/>extract both to same OUT path",
            rendered,
        )
        self.assertNotIn("\nextract both to same OUT path\n", rendered)

    def test_wrapped_note_remainder_is_rejected_before_repair(self):
        result = validate_sequence_mermaid_offline(
            self.WRAPPED_NOTE_DIAGRAM,
            treat_unknown_as_error=True,
        )

        self.assertFalse(result["is_valid"])
        self.assertEqual(result["details"]["implicit_participants"], [])

    def test_prose_without_a_preceding_note_degrades_the_whole_diagram(self):
        rendered = format_mermaid(
            "sequenceDiagram\n"
            "participant A\n"
            "participant B\n"
            "A->>B: call\n"
            "extract both to same OUT path"
        )

        self.assertEqual(rendered, "")

    def test_prose_containing_arrow_characters_is_unknown_syntax(self):
        for prose in (
            "extract both to same OUT path",
            "both to same OUT path",
            "context is not available",
            "before the merge",
            "fix the collision first",
            "no reachable consumer",
        ):
            with self.subTest(prose=prose):
                result = validate_sequence_mermaid_offline(
                    f"sequenceDiagram\nparticipant A\nA->>A: go\n{prose}",
                    treat_unknown_as_error=True,
                )

                self.assertFalse(result["is_valid"])
                self.assertEqual(result["details"]["implicit_participants"], [])

    def test_every_supported_arrow_still_renders(self):
        for arrow in sorted(ALLOWED_ARROWS_SUPERSET):
            with self.subTest(arrow=arrow):
                rendered = format_mermaid(
                    "sequenceDiagram\n"
                    "participant A\n"
                    "participant B\n"
                    f"A{arrow}B: payload"
                )

                self.assertIn(f"A{arrow}B: payload", rendered)

    def test_participant_ending_in_an_arrow_character_keeps_the_real_split(self):
        result = validate_sequence_mermaid_offline(
            "sequenceDiagram\nparticipant Ao\nparticipant B\nAo-->B: call",
            treat_unknown_as_error=True,
        )

        self.assertTrue(result["is_valid"])
        self.assertEqual(result["details"]["implicit_participants"], [])


if __name__ == "__main__":
    unittest.main()
