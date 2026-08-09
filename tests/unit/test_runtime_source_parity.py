import os
import unittest
from pathlib import Path

from tests.unit.fakes import ensure_repo_root_on_path

ensure_repo_root_on_path()

from scripts.verify_runtime_parity import (  # noqa: E402
    BASELINE_COMMIT,
    EXPECTED_MODIFIED,
    EXPECTED_REMOVED,
    build_report,
    compare_sources,
)


class RuntimeSourceParityTest(unittest.TestCase):
    def test_classifier_rejects_an_unrecorded_runtime_change(self):
        report = compare_sources(
            {"runtime.py": b"accepted\n"},
            {"runtime.py": b"changed\n"},
            expected_modified={},
            expected_removed={},
        )

        self.assertFalse(report["passed"])
        self.assertEqual(
            report["unexplained_differences"][0]["reason"],
            "unclassified runtime source difference",
        )

    def test_classifier_accepts_only_an_exact_recorded_modification(self):
        baseline = b"accepted\n"
        public = b"intentional\n"
        rule = {
            "runtime.py": {
                "classification": "test_boundary",
                "baseline_sha256": (
                    "4825c38ba9e071bc3e19961e7c1bd0c1a2fcc575a5cff7e416d7f7c772597271"
                ),
                "public_sha256": (
                    "4a59f6bdc4f657508f8c8390817743a19df77120b7fc1fe46dcbdf34a93ef719"
                ),
            }
        }

        report = compare_sources(
            {"runtime.py": baseline},
            {"runtime.py": public},
            expected_modified=rule,
            expected_removed={},
        )

        self.assertTrue(report["passed"])
        self.assertEqual(
            report["intentional_differences"][0]["classification"],
            "test_boundary",
        )

    def test_migration_allowlist_is_small_and_explicit(self):
        self.assertEqual(
            set(EXPECTED_MODIFIED),
            {
                "lambdas/LlamaPReviewPipeline/config.py",
                "lambdas/LlamaPReviewPipeline/persistence.py",
                "lambdas/LlamaPReviewPipeline/pipeline_admission.py",
                "lambdas/LlamaPReviewPipeline/pipeline_accounting.py",
                "lambdas/LlamaPReviewPipeline/provider_model_routing.py",
                "lambdas/LlamaPReviewPipeline/review/publish.py",
                "lambdas/LlamaPReviewWebhookHandler/lambda_function.py",
            },
        )
        self.assertEqual(
            set(EXPECTED_REMOVED),
            {
                "lambdas/LlamaPReviewWebhookHandler/README.md",
                "lambdas/LlamaPReviewWebhookHandler/legacy_repo_insights.py",
            },
        )
        self.assertEqual(
            BASELINE_COMMIT,
            "cd7edc5eb6a1f83b322c6314405fd72b57546114",
        )
        for path in (
            "lambdas/LlamaPReviewPipeline/pipeline_accounting.py",
            "lambdas/LlamaPReviewPipeline/provider_model_routing.py",
        ):
            self.assertEqual(
                EXPECTED_MODIFIED[path]["verification"],
                "python_ast_without_docstrings_equal",
            )

    @unittest.skipUnless(
        os.environ.get("LLAMAPREVIEW_BASELINE_REPO"),
        "set LLAMAPREVIEW_BASELINE_REPO to run exact migration parity",
    )
    def test_exact_baseline_has_no_unexplained_runtime_difference(self):
        report = build_report(Path(os.environ["LLAMAPREVIEW_BASELINE_REPO"]))

        self.assertTrue(report["passed"], report["unexplained_differences"])
        self.assertEqual(report["baseline_read_mode"], "exact_git_object")
        self.assertEqual(len(report["intentional_differences"]), 9)


if __name__ == "__main__":
    unittest.main()
