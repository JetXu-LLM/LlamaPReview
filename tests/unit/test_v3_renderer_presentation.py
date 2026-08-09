import unittest

from lambdas.LlamaPReviewPipeline.review.render import render_v3_markdown


SANITIZED_NONINLINE_REPLAYS = {
    # Sanitized structural shapes only; no model trace or private reasoning.
    "case-01": {
        "headline": "A retained non-blocking edge case needs maintainer attention",
        "comment": "The retained edge case can change the fallback result.",
        "suggestion_type": "CONCEPTUAL_ADVICE",
    },
    "case-02": {
        "headline": "The retained configuration path has a concrete follow-up",
        "comment": "The changed configuration leaves the retained path inconsistent.",
        "suggestion_type": "DIRECT_REPLACEMENT",
    },
    "case-03": {
        "headline": "The retained behavior has a discoverable correction",
        "comment": "The existing correction remains actionable in the changed context.",
        "suggestion_type": "DIRECT_REPLACEMENT",
    },
    "case-04": {
        "headline": "The retained asset behavior needs a bounded correction",
        "comment": "The changed asset behavior can resolve to the wrong packaged path.",
        "suggestion_type": "CONCEPTUAL_ADVICE",
    },
    "case-05": {
        "headline": "The retained gateway path has a concrete compatibility concern",
        "comment": "The changed gateway path can bypass the retained compatibility behavior.",
        "suggestion_type": "CONCEPTUAL_ADVICE",
    },
}


def finding(
    *,
    finding_id="F1",
    priority="P2",
    visibility="collapsed",
    blocking=False,
    headline="The retained caller path remains compatible",
    comment=(
        "The changed contract still matches the retained caller and "
        "preserves its fallback behavior."
    ),
    suggested_code="",
    suggestion_type=None,
):
    item = {
        "id": finding_id,
        "finding_type": "note",
        "priority": priority,
        "confidence": "High",
        "evidence_status": "verified",
        "claim_scope": "bounded_context",
        "blocking": blocking,
        "visibility": visibility,
        "headline": headline,
        "file_path": "src/service.py",
        "code_snippet": "return fallback(value)",
        "comment": comment,
        "evidence_refs": ["ev_supporting_contract"],
    }
    if suggested_code:
        item["suggested_code"] = suggested_code
        item["suggestion_type"] = suggestion_type
    return item


def review(
    *,
    verdict="clear",
    public_sentence="No review blocker found in the reviewed changes.",
    findings=None,
    reasons=None,
    actions=None,
    unknowns=None,
    scope=None,
    diagram=None,
):
    return {
        "visible_verdict": verdict,
        "decision": {
            "public_sentence": public_sentence,
            "reasons": reasons or [],
        },
        "owner_action": actions or [],
        "findings": findings or [],
        "material_unknowns": unknowns or [],
        "evidence_scope": scope or [],
        "diagram": diagram,
    }


class V3RendererPresentationTests(unittest.TestCase):
    def test_overlong_first_screen_never_ends_as_a_fragment(self):
        long_sentence = " ".join(
            ["The changed workflow remains safe because"]
            + ["validated"] * 50
        ) + "."
        rendered = render_v3_markdown(
            review(public_sentence=f"No review blocker found. {long_sentence}")
        )

        self.assertNotIn("…", rendered.split("<details>", 1)[0])
        self.assertNotIn("validated validated validated", rendered)
        self.assertIn(
            "Reviewed only the available changed regions; no broader coverage is claimed.",
            rendered,
        )

    def test_clean_useful_model_sentence_is_preserved(self):
        rendered = render_v3_markdown(
            review(
                public_sentence=(
                    "No review blocker found. The changed contract and retained "
                    "caller path remain consistent."
                )
            )
        )

        self.assertIn(
            "The changed contract and retained caller path remain consistent.",
            rendered,
        )
        self.assertNotIn("Reviewed only the available changed regions", rendered)

    def test_filecryption_736_scope_replay_uses_guarded_check_as_clear_proof(self):
        replay_id = "Filecryption#736"
        rendered = render_v3_markdown(
            review(
                scope=[
                    {
                        "evidence_ref": "ev_scope_detail",
                        "description": (
                            "Read the complete PR-head files `Cargo.toml` and "
                            "`Cargo.lock`."
                        ),
                    }
                ]
            )
        )
        first_screen = rendered.split("<details>", 1)[0]

        with self.subTest(replay=replay_id):
            self.assertIn(
                "Read the complete PR-head files `Cargo.toml` and `Cargo.lock`.",
                first_screen,
            )
        self.assertNotIn("No review blocker found", first_screen)
        self.assertNotIn("ev_scope_detail", rendered)

    def test_nonblocking_replay_is_not_visually_empty(self):
        replay_id = "case-01"
        retained = finding(
            headline="The compatibility fallback is now bypassed",
            comment=(
                "The new branch returns before the existing compatibility "
                "fallback can run."
            ),
        )
        rendered = render_v3_markdown(
            review(
                findings=[retained],
                reasons=[
                    {
                        "text": retained["headline"],
                        "refs": ["F1"],
                    }
                ],
            )
        )
        first_screen = rendered.split("<details>", 1)[0]

        with self.subTest(replay=replay_id):
            self.assertIn(
                "1 non-blocking finding retained — highest: "
                "The compatibility fallback is now bypassed.",
                first_screen,
            )
        self.assertNotIn("F1", rendered)
        self.assertNotIn("ev_supporting_contract", rendered)

    def test_causal_model_proof_precedes_nonblocking_finding_count(self):
        rendered = render_v3_markdown(
            review(
                public_sentence=(
                    "No review blocker found. The observed contract remains "
                    "internally consistent."
                ),
                findings=[
                    finding(
                        priority="P1",
                        headline="The retained fallback is bypassed",
                    )
                ],
            )
        )
        first_screen = rendered.split("<details>", 1)[0]

        self.assertIn(
            "1 non-blocking finding retained — highest: "
            "The retained fallback is bypassed.",
            first_screen,
        )
        self.assertIn(
            "The observed contract remains internally consistent.",
            first_screen,
        )
        self.assertLess(
            first_screen.index(
                "The observed contract remains internally consistent."
            ),
            first_screen.index("1 non-blocking finding retained"),
        )

    def test_low_context_clear_review_remains_conservative(self):
        rendered = render_v3_markdown(review())

        self.assertEqual(
            rendered,
            "### LlamaPReview — No blocking issues found\n\n"
            "Reviewed only the available changed regions; no broader coverage is claimed.",
        )
        self.assertNotIn("repository-wide", rendered)
        self.assertNotIn("tests passed", rendered)
        self.assertNotIn("runtime verified", rendered)

    def test_retained_finding_count_is_arithmetic_for_zero_through_eight(self):
        for count in range(9):
            retained = [
                finding(
                    finding_id=f"F{index + 1}",
                    headline=f"Retained finding {index + 1}",
                )
                for index in range(count)
            ]
            rendered = render_v3_markdown(
                review(
                    public_sentence="No review blocker found in the reviewed changes.",
                    findings=retained,
                )
            )
            first_screen = rendered.split("<details>", 1)[0]
            with self.subTest(count=count):
                if count == 0:
                    self.assertNotIn("non-blocking finding retained", first_screen)
                    self.assertNotIn("non-blocking findings retained", first_screen)
                else:
                    plural = "" if count == 1 else "s"
                    self.assertIn(
                        f"{count} non-blocking finding{plural} retained — highest: "
                        "Retained finding 1.",
                        first_screen,
                    )

    def test_clear_fold_is_one_proof_unit_without_reason_bullets(self):
        rendered = render_v3_markdown(
            review(
                public_sentence=(
                    "No review blocker found. The changed branch preserves "
                    "the retained fallback."
                ),
                reasons=[
                    {
                        "text": "The existing caller still reaches the fallback.",
                        "refs": [],
                    }
                ],
            )
        )
        first_screen = rendered.split("<details>", 1)[0]

        self.assertIn(
            "The changed branch preserves the retained fallback.",
            first_screen,
        )
        self.assertNotIn(
            "- The existing caller still reaches the fallback.",
            first_screen,
        )

    def test_clear_model_proof_keeps_first_bounded_causal_sentence(self):
        scope = [
            {
                "description": "Reviewed the changed producer and its caller.",
            }
        ]
        rendered = render_v3_markdown(
            review(
                public_sentence=(
                    "No review blocker found. The changed branch preserves "
                    "the request ordering. Secondary explanation remains "
                    "below the fold."
                ),
                scope=scope,
            )
        )
        first_screen = rendered.split("<details>", 1)[0]

        self.assertIn(
            "The changed branch preserves the request ordering.",
            first_screen,
        )
        self.assertNotIn("Secondary explanation", first_screen)
        self.assertNotIn(scope[0]["description"], first_screen)

        oversized = "x" * 361
        fallback = render_v3_markdown(
            review(
                public_sentence=f"No review blocker found. {oversized}",
                scope=scope,
            )
        ).split("<details>", 1)[0]
        self.assertIn(scope[0]["description"], fallback)
        self.assertNotIn(oversized, fallback)

    def test_clear_green_ci_phrases_are_never_first_screen_proof(self):
        phrases = (
            "All checks passed",
            "CI checks pass",
            "CI pipeline passes",
            "Passed all test jobs",
            "The Mutation check and all test matrices pass",
            "The build is green",
        )
        scope = [{"description": "Reviewed the changed producer and caller."}]
        for phrase in phrases:
            base = review(
                public_sentence=f"No review blocker found. {phrase}.",
                scope=scope,
            )
            rendered = render_v3_markdown(base)
            with self.subTest(phrase=phrase, case="no_failed_ci_finding"):
                self.assertNotIn(phrase, rendered.split("<details>", 1)[0])
                self.assertIn(scope[0]["description"], rendered)

            retained = finding(headline="The failed diagnostic is non-blocking")
            retained["evidence_refs"] = ["ci:check_run:42"]
            backed = review(
                public_sentence=f"No review blocker found. {phrase}.",
                findings=[retained],
            )
            backed["ci_failed_evidence_refs"] = ["ci:check_run:42"]
            rendered = render_v3_markdown(backed)
            with self.subTest(phrase=phrase, case="failed_ci_backed"):
                self.assertNotIn(phrase, rendered.split("<details>", 1)[0])
                self.assertIn(
                    "The failed diagnostic is non-blocking",
                    rendered.split("<details>", 1)[0],
                )

            unrelated = review(
                public_sentence=f"No review blocker found. {phrase}.",
                findings=[retained],
                scope=scope,
            )
            unrelated["ci_failed_evidence_refs"] = ["ci:check_run:99"]
            rendered = render_v3_markdown(unrelated)
            with self.subTest(phrase=phrase, case="different_failed_check"):
                self.assertNotIn(phrase, rendered.split("<details>", 1)[0])

    def test_clear_trailing_green_ci_clause_preserves_causal_proof(self):
        scope = [{"description": "Read the complete PR-head package file."}]
        rendered = render_v3_markdown(
            review(
                public_sentence=(
                    "No review blocker found. Lockfile-only minor bump to "
                    "tailwind-merge 3.6.0; manifest-compatible, no API "
                    "changes, and both CI checks pass. Safe to merge as-is."
                ),
                scope=scope,
            )
        )
        first_screen = rendered.split("<details>", 1)[0]

        self.assertIn(
            "Lockfile-only minor bump to tailwind-merge 3.6.0; "
            "manifest-compatible, no API changes.",
            first_screen,
        )
        self.assertNotIn("CI checks pass", first_screen)
        self.assertNotIn(scope[0]["description"], first_screen)

    def test_clear_embedded_green_ci_clause_preserves_independent_causal_proof(self):
        cases = (
            (
                "Routine dev-only dependency bump fixes a security advisory, "
                "passes all CI checks, and introduces no behavioral surface change.",
                ("fixes a security advisory", "introduces no behavioral surface change"),
            ),
            (
                "Safe to merge: the console feature is internally consistent, "
                "CI is green across all builds and static analysis, and the two "
                "flagged edge cases are cosmetic rather than PR-caused blockers.",
                ("console feature is internally consistent", "edge cases are cosmetic"),
            ),
            (
                "The lockfile bump carries upstream security fixes; supplied CI "
                "is green and no reachable regression path is supported.",
                ("carries upstream security fixes", "no reachable regression path"),
            ),
            (
                "The demo book falls back per symbol so it cannot be blanked; "
                "all consumers remain safe and the full CI suite is green at head.",
                ("cannot be blanked", "all consumers remain safe"),
            ),
        )
        scope = [{"description": "Reviewed only a fallback scope."}]
        for sentence, retained in cases:
            with self.subTest(sentence=sentence):
                first_screen = render_v3_markdown(
                    review(
                        public_sentence=f"No review blocker found. {sentence}",
                        scope=scope,
                    )
                ).split("<details>", 1)[0]
                for phrase in retained:
                    self.assertIn(phrase, first_screen)
                self.assertNotRegex(
                    first_screen.casefold(),
                    r"\b(?:ci|checks?)\b.*\b(?:pass|green)",
                )
                self.assertNotIn(scope[0]["description"], first_screen)

    def test_template_artifact_fixtures_fall_back_without_touching_clean_copy(self):
        repeated = "one two three four five six seven eight nine ten"
        artifacts = (
            "whether whether the fallback applies",
            "whether how the fallback applies",
            "whether not all callers are covered",
            "First action.; second action",
            (
                "Could not verify whether the caller could not verify whether "
                "the fallback applies"
            ),
            f"{repeated} and then {repeated}",
        )
        for artifact in artifacts:
            rendered = render_v3_markdown(
                review(
                    public_sentence=f"No review blocker found. {artifact}",
                )
            )
            with self.subTest(artifact=artifact):
                self.assertNotIn(artifact, rendered)
                self.assertIn(
                    "Reviewed only the available changed regions",
                    rendered,
                )

        clean = "The changed producer preserves the caller's fallback."
        rendered = render_v3_markdown(
            review(public_sentence=f"No review blocker found. {clean}")
        )
        self.assertIn(clean, rendered)

    def test_composed_fold_lints_cross_field_repeated_span(self):
        repeated = "verify the current producer terminal result before merging this change"
        unknown = {
            "id": "U1",
            "claim": f"We must {repeated}.",
            "how_to_check": f"Please {repeated} in the queued-head environment.",
            "affects_merge": True,
            "evidence_refs": [],
        }
        rendered = render_v3_markdown(
            review(
                verdict="unverified",
                public_sentence="Merge readiness still needs verification.",
                unknowns=[unknown],
                actions=[
                    {
                        "text": unknown["how_to_check"],
                        "resolves": ["U1"],
                    }
                ],
            )
        )
        first_screen = rendered.split("<details>", 1)[0]

        self.assertEqual(first_screen.casefold().count(repeated), 1)
        self.assertNotIn("Owner action:", first_screen)
        self.assertIn(unknown["how_to_check"], rendered)

    def test_noninline_finding_keeps_explanation_boundary_and_safe_conceptual_code(self):
        suggested_code = (
            "if enabled:\n"
            "    note = \"embedded fence follows\"\n"
            "    ````\n"
            "    return fallback(value)"
        )
        retained = finding(
            comment=(
                "Returning here skips the retained fallback, so existing callers "
                "can receive an empty result."
            ),
            suggested_code=suggested_code,
            suggestion_type="CONCEPTUAL_ADVICE",
        )
        rendered = render_v3_markdown(review(findings=[retained]))

        self.assertIn(
            "Returning here skips the retained fallback, so existing callers "
            "can receive an empty result.",
            rendered,
        )
        self.assertIn(
            "Verification boundary: confirmed; scope: bounded reviewed context.",
            rendered,
        )
        self.assertIn(
            "Conceptual guidance (not a committable GitHub suggestion)",
            rendered,
        )
        self.assertIn("`````\n" + suggested_code + "\n`````", rendered)
        self.assertNotIn("```suggestion", rendered)

    def test_direct_replacement_is_labeled_without_changing_its_code(self):
        suggested_code = "return fallback(value)"
        rendered = render_v3_markdown(
            review(
                findings=[
                    finding(
                        suggested_code=suggested_code,
                        suggestion_type="DIRECT_REPLACEMENT",
                    )
                ]
            )
        )

        self.assertIn("**Suggested direct replacement:**", rendered)
        self.assertIn("```\n" + suggested_code + "\n```", rendered)

    def test_sanitized_noninline_replays_keep_explanations_and_suggestions(self):
        suggested_code = "return retained_behavior(value)"
        for replay_id, shape in SANITIZED_NONINLINE_REPLAYS.items():
            with self.subTest(replay=replay_id):
                rendered = render_v3_markdown(
                    review(
                        findings=[
                            finding(
                                headline=shape["headline"],
                                comment=shape["comment"],
                                suggested_code=suggested_code,
                                suggestion_type=shape["suggestion_type"],
                            )
                        ]
                    )
                )
                self.assertIn(shape["headline"], rendered)
                self.assertIn(shape["comment"], rendered)
                self.assertIn(suggested_code, rendered)
                if shape["suggestion_type"] == "DIRECT_REPLACEMENT":
                    self.assertIn("Suggested direct replacement", rendered)
                else:
                    self.assertIn(
                        "Conceptual guidance "
                        "(not a committable GitHub suggestion)",
                        rendered,
                    )

    def test_inline_finding_is_summarized_but_not_duplicated_in_details(self):
        inline_comment = (
            "This full explanation belongs in the anchored inline comment only."
        )
        inline_code = "return corrected_value"
        rendered = render_v3_markdown(
            review(
                findings=[
                    finding(
                        visibility="inline",
                        headline="The returned value violates the changed contract",
                        comment=inline_comment,
                        suggested_code=inline_code,
                        suggestion_type="DIRECT_REPLACEMENT",
                    )
                ]
            )
        )

        self.assertIn("The returned value violates the changed contract", rendered)
        self.assertNotIn(inline_comment, rendered)
        self.assertNotIn(inline_code, rendered)
        self.assertNotIn("### Finding details", rendered)

    def test_unverified_replay_has_one_explanation_and_action(self):
        replay_id = "case-06"
        unknowns = [
            {
                "id": "U1",
                "claim": "The new asset paths were not verified.",
                "how_to_check": "Open both assets from the packaged application.",
                "affects_merge": True,
            },
            {
                "id": "U2",
                "claim": "The generated bundle contents remain unknown.",
                "how_to_check": "Inspect the generated bundle manifest.",
                "affects_merge": True,
            },
        ]
        rendered = render_v3_markdown(
            review(
                verdict="unverified",
                public_sentence=(
                    "Merge readiness still needs verification: "
                    "the new asset paths were not verified."
                ),
                reasons=[
                    {"text": unknowns[0]["claim"], "refs": ["U1"]},
                    {"text": unknowns[1]["claim"], "refs": ["U2"]},
                ],
                actions=[
                    {
                        "text": unknowns[0]["how_to_check"],
                        "resolves": ["U1"],
                    },
                    {
                        "text": unknowns[1]["how_to_check"],
                        "resolves": ["U2"],
                    },
                ],
                unknowns=unknowns,
            )
        )
        first_screen = rendered.split("<details>", 1)[0]
        details = rendered.split("<details>", 1)[1]

        with self.subTest(replay=replay_id):
            self.assertEqual(first_screen.count("Owner action:"), 1)
        self.assertIn(unknowns[0]["how_to_check"], first_screen)
        self.assertNotIn(unknowns[1]["how_to_check"], first_screen)
        self.assertNotIn(f"- {unknowns[0]['claim']}", first_screen)
        self.assertNotIn(f"- {unknowns[1]['claim']}", first_screen)
        self.assertIn(unknowns[0]["claim"], details)
        self.assertIn(unknowns[1]["claim"], details)
        self.assertNotIn(unknowns[0]["how_to_check"], details)
        self.assertIn(unknowns[1]["how_to_check"], details)
        self.assertNotIn("U1", rendered)
        self.assertNotIn("U2", rendered)

    def test_unverified_action_falls_back_to_structured_check(self):
        rendered = render_v3_markdown(
            review(
                verdict="unverified",
                public_sentence="",
                unknowns=[
                    {
                        "id": "U1",
                        "claim": "The deployment contract remains unverified.",
                        "how_to_check": "Run the existing deployment contract check.",
                        "affects_merge": True,
                    }
                ],
            )
        )

        self.assertIn(
            "The deployment contract remains unverified.",
            rendered,
        )
        self.assertIn(
            "Owner action: Run the existing deployment contract check.",
            rendered,
        )

    def test_blocker_and_mermaid_presentation_do_not_regress(self):
        blocker = finding(
            priority="P1",
            visibility="headline",
            blocking=True,
            headline="The migration drops retained state",
        )
        mermaid = "```mermaid\nsequenceDiagram\n  A->>B: migrate\n```"
        rendered = render_v3_markdown(
            review(
                verdict="blocked_findings",
                public_sentence="Do not merge until the retained state is preserved.",
                findings=[blocker],
                reasons=[
                    {
                        "text": "The migration drops retained state.",
                        "refs": ["F1"],
                    }
                ],
                actions=[
                    {
                        "text": "Preserve the retained state before merging.",
                        "resolves": ["F1"],
                    }
                ],
                diagram={
                    "purpose": "risk_path",
                    "description": "The migration bypasses retained state.",
                    "mermaid": mermaid,
                    "finding_refs": ["F1"],
                    "evidence_refs": ["ev_supporting_contract"],
                },
            )
        )

        self.assertIn("### LlamaPReview — Blocking issues found", rendered)
        self.assertIn("- The migration drops retained state.", rendered)
        self.assertIn(
            "Owner action: Preserve the retained state before merging.",
            rendered,
        )
        self.assertEqual(rendered.count(mermaid), 1)
        self.assertNotIn("F1", rendered)
        self.assertNotIn("ev_supporting_contract", rendered)


if __name__ == "__main__":
    unittest.main()
