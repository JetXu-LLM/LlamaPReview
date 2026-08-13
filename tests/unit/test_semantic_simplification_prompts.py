import unittest

from tests.unit.fakes import ensure_repo_root_on_path

ensure_repo_root_on_path()

from lambdas.LlamaPReviewPipeline.review import prompts


class TestSemanticSimplificationPrompts(unittest.TestCase):
    def test_deep_is_free_form_sole_judgment_with_three_review_passes(self):
        prompt = prompts.DEEP_JUDGMENT_PROMPT

        for phrase in (
            "sole engineering judgment authority",
            "Raw-diff discovery",
            "Context synthesis",
            "Falsification",
            "benign premise",
            "PFR is\n   non-exhaustive evidence acquisition",
            "Good is descriptive",
            "Great is causal",
            "Trace work in execution order",
            "Work already\ncompleted before routing, dispatch, fallback, or state selection",
            "do not assume traffic frequency, deployment settings, external speed",
            "do not infer a missing guard from one local\nhandler",
            "An unobserved security, authentication, deployment, or environment premise",
            "Treat mutable CI as PR evidence, not as a repository-policy proxy",
            'A label such\nas "quality gate" or a configured threshold proves the reported metric outcome',
            "not that repository owners require that check for merge",
            "merely possible required-check policy stays non-code-blocking",
            "do not turn it into a finding, material unknown, merge gate",
            "only as a confidence-changing check",
            "Every\nrequest-changes reason, including a merge-deciding P2",
            "admitted exact-head\nevidence for its changed mechanism and causal consequence",
            "A missing or uncertain inline anchor changes placement",
            "do not relabel that action as a post-merge follow-up merely",
            "request changes and name the P2 that carries that",
            "A concern whose causal premise remains unobserved is not a pre-merge action",
            "merely because an owner could accept, document, threat-model, run a check",
            "Keep that concern nonblocking and out of the\nrequest-changes",
            "A request-changes reason must rest on an observed",
            "an unperformed check that could reveal a problem does not establish one",
            "do not append a request to\nverify them to the blocking sentence",
            "does not decide\nthe merge posture",
            "Cluster related P2 observations",
            "positive\nor falsified hypotheses with no remaining risk",
            "named local import",
            "only a genuinely unavailable fact\nremains an honest gap",
            "requested-but-unreturned symbol\nbody is a coverage gap",
            "never proof that a caller, use, implementation, or\nrepository path does not exist",
            "one overall High, Medium, or Low decision\nconfidence",
            "Reserve P0 for an actively exploitable security breach",
            "ordinary PR-head compile, test, startup, or runtime-path\nfailure is P1",
            "include an `Evidence refs:` line containing only exact catalog IDs",
            "prose label is explanation, never an evidence reference",
            "one evidence-bound core changed flow",
            "complete cross-channel or\ncross-module workflow may extend beyond changed lines",
            "If removing the PR change\nwould leave essentially the same architecture-documentation picture",
            "Visual: useful",
            "Visual: not useful",
            "important maintainer question faster than prose",
            "End a useful visual judgment with its own `Evidence refs:` line",
            "If no\nexact catalog evidence supports the picture, it is not useful",
            "Do not write Mermaid in Deep",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, prompt)

        self.assertEqual(prompt.count("upstream outcome"), 1)
        self.assertEqual(prompt.count("actor-selected"), 1)
        self.assertEqual(prompt.count("PR-created causal delta"), 1)
        self.assertNotIn("submit_final_judgment", prompt)
        self.assertNotIn("C*/U*", prompt)
        self.assertNotIn("obl_", prompt)
        self.assertNotIn("disposition", prompt.lower())

    def test_deep_renderer_preserves_and_labels_each_supplied_surface(self):
        rendered = prompts.render_deep_judgment_prompt(
            "intent Ω",
            "context λ",
            acceptance_criteria=["must preserve callers"],
            changed_delta={"files": ["src/app.py"]},
            ci_snapshot={"head": "a" * 40},
            evidence_catalog=[{"id": "ev_1"}],
            evidence_gaps=[{"fact": "runtime contract"}],
        )

        for text in (
            "intent Ω",
            "context λ",
            '"must preserve callers"',
            '"src/app.py"',
            '"ev_1"',
            '"runtime contract"',
            "<EXACT_HEAD_CI_UNTRUSTED>",
            "<HONEST_EVIDENCE_GAPS_UNTRUSTED>",
        ):
            with self.subTest(text=text):
                self.assertIn(text, rendered)

    def test_final_has_one_fixed_presentation_only_transport(self):
        prompt = prompts.FINAL_PRESENTATION_PROMPT

        for field in (
            '"version": "presentation_v1"',
            '"confidence": "High|Medium|Low"',
            '"required_evidence_refs"',
            '"supporting_evidence_refs"',
            '"material_unknowns"',
            '"confidence_checks"',
            '"owner_action": "Concrete Deep-derived action for this finding."',
            '"placement": "inline|headline|collapsed"',
            "`DIRECT_REPLACEMENT`",
            "at most two\nblocking headlines, four inline findings, and one nonblocking inline",
        ):
            with self.subTest(field=field):
                self.assertIn(field, prompt)

        normalized_prompt = " ".join(prompt.split()).casefold()
        for boundary in (
            "Deep is the substantive authority",
            "do not omit a material Deep conclusion",
            "Copy `decision.confidence` from Deep without recalibrating it",
            "Copy Deep's explicit opening merge posture without inferring it from later unknowns or checks",
            "When Deep says approve, clear, or no blocking findings, emit `clear`",
            "when Deep says request changes or do not merge, emit `blocking`",
            "Emit `verification_needed` only when Deep's explicit opening posture itself says not to merge",
            "A later nonblocking unknown or optional check never qualifies",
            "Never pair `clear` or `verification_needed` with P0/P1",
            "keep in `material_unknowns` only facts Deep explicitly said decide the merge posture",
            "Preserve other uncertainty as a `confidence_checks` item instead",
            "never make a nondeciding check merge-affecting",
            "may mention only the findings Deep explicitly named as merge-posture carriers",
            "Keep nonblocking findings, nondeciding material unknowns, and confidence checks out of that first-screen decision",
            "If Deep's opening paragraph mixes a blocking carrier with a separate nondeciding unknown or check",
            "while the verdict remains blocking is nondeciding",
            "Every output item must be traceable to Deep",
            "Only copy IDs that Deep explicitly listed on an `Evidence refs:` line",
            "never turn a path, symbol, check name, or other prose description into an evidence reference",
            "retained nonblocking P2 may have an empty required array",
            "at least one of the two evidence arrays must contain an exact admitted reference",
            "otherwise omit that nonblocking item without inventing a reference",
            "every blocking finding needs at least one admitted required",
            "confidence-changing checks as an exclusive source section",
            "exactly one output home: `confidence_checks`",
            "causal-attribution explanation, or any other substance",
            "`decision.summary`, `decision.owner_actions`, `material_unknowns`",
            "exact reference required evidence for that finding's causal conclusion",
            "never by CI check name or other name heuristics",
            "Aim for at most 8 findings, at most 8 material unknowns, and at most 6 confidence checks",
            "positive observation with no remaining risk or owner action",
            "Use `test-gap` only for missing or insufficient coverage",
            "one or more complete, verbatim, contiguous post-change/current-head source lines",
            "never crop a source line to an internal substring",
            "Never include unified-diff marker prefixes or any removed (`-`) line",
            "select only complete context and added-side lines that form one contiguous current-head span",
            "character-for-character after JSON decoding",
            "preserving every line's leading whitespace, including the first line",
            "control characters with standard JSON escapes",
            "never place a literal control character inside a JSON string",
            "Never combine before-change and after-change alternatives",
            "Never use `...` or another synthetic omission placeholder",
            "Code will assign public identities",
        ):
            with self.subTest(boundary=boundary):
                self.assertIn(
                    " ".join(boundary.split()).casefold(),
                    normalized_prompt,
                )

        self.assertNotIn("semantic closure", prompt.lower())
        self.assertNotIn("obligation", prompt.lower())
        self.assertNotIn("disposition", prompt.lower())
        self.assertNotIn("submit_final_judgment", prompt)

        for visual_rule in (
            "Deep owns the visual judgment",
            "respect its explicit `Visual: useful` or `Visual: not useful` decision",
            "creation, significant refactoring, or modification of a core system process",
            "show the complete relevant workflow from entry to exit",
            "explicitly highlight the core modification",
            "cross channels or modules outside the changed files",
            "cross-component or multi-stage behavior does not need to be cross-system",
            "3–6 short human-readable participant aliases",
            "5–12 meaningful messages",
            "PR change —",
            "Impact —",
            "specific unsafe changed slice—not the entire diagram—inside a GitHub-safe `critical ... end` block",
            "without an immediately visible `PR change` highlight has not earned the first screen",
            "architecture documentation rather than PR-review value and must be null",
            "`alt`, `break`, or `opt`",
            "simple ID plus a clean human-readable alias",
            "Every note must occupy one physical source line",
            "visible semicolon as `#59;`",
            "visible hash as `#35;`",
            "raw prose outside Mermaid statements",
            "When Deep explicitly names a blocking risk path, use `risk_path`, not `pr_flow_map`",
            "Every non-null diagram must copy at least one exact catalog ID from the useful visual's `Evidence refs:` line",
        ):
            with self.subTest(visual_rule=visual_rule):
                self.assertIn(
                    " ".join(visual_rule.split()),
                    " ".join(prompt.split()),
                )

        for presentation_rule in (
            "For a clear verdict, make `decision.summary` say why the changed behavior is safe to merge",
            "Keep that sentence independent of optional findings or checks",
            "do not copy their unsupported premise into the first screen",
            "Do not turn an optional follow-up, nondeciding unknown, or confidence check into a precondition",
            "A public finding is an unresolved PR-caused consequence that deserves owner attention",
            "Choose `inline` only when the exact changed line gives the owner an immediate local action or repair",
            "material it explicitly labels cosmetic, a future design note, or an optional refactor is not a finding",
            "For a code-caused blocker, its required array must include exact changed-code evidence from Deep's `Evidence refs:` line",
            "never leave the sole changed-code proof only in `supporting_evidence_refs`",
            "A non-source PR-metadata or policy blocker may instead use an exact causal CI diagnostic",
        ):
            with self.subTest(presentation_rule=presentation_rule):
                self.assertIn(
                    " ".join(presentation_rule.split()).casefold(),
                    normalized_prompt,
                )

    def test_final_renderer_supplies_only_bounded_changed_delta_context(self):
        rendered = prompts.render_final_presentation_prompt(
            {
                "schema": "llamapreview.changed_delta_focus.v1",
                "files": [{"path": "src/flow.py", "patch": "+dispatch()"}],
            }
        )

        self.assertIn("<PRESENTATION_CHANGED_DELTA_UNTRUSTED>", rendered)
        self.assertIn("src/flow.py", rendered)
        self.assertIn("+dispatch()", rendered)
        self.assertIn("not authority to add or change a finding", rendered)

if __name__ == "__main__":
    unittest.main()
