import unittest

from tests.unit.fakes import ensure_repo_root_on_path, set_default_env

ensure_repo_root_on_path()
set_default_env()

from lambdas.LlamaPReviewPipeline.repository_paths import (
    bounded_read_opt_in,
    is_ci_config_path,
    is_dependency_lock_path,
    is_dependency_manifest_path,
)

class RepositoryPathContractsTest(unittest.TestCase):
    def test_dependency_identity_is_shared_across_standard_families(self):
        for path in (
            "requirements.txt",
            "requirements-dev.txt",
            "requirements_test.in",
            "uv.lock",
            "go.sum",
            "gradle.lockfile",
            "packages.lock.json",
            "pnpm-lock.yml",
            "nested/custom.lock.json",
        ):
            with self.subTest(path=path):
                self.assertTrue(is_dependency_manifest_path(path))
        self.assertFalse(is_dependency_manifest_path("docs/release-notes.txt"))
        self.assertFalse(is_dependency_lock_path("src/deadlock.py"))

    def test_bounded_read_opt_in_is_narrow_and_exact(self):
        for path in ("uv.lock", "go.sum", "packages.lock.json"):
            with self.subTest(path=path):
                self.assertEqual(bounded_read_opt_in(path), "dependency_lock")
        for path in (
            ".github/workflows/test.yml",
            ".gitlab/ci/release.yml",
            "appveyor.yaml",
            ".travis.yaml",
        ):
            with self.subTest(path=path):
                self.assertTrue(is_ci_config_path(path))
                self.assertEqual(bounded_read_opt_in(path), "ci_config")
        self.assertIsNone(bounded_read_opt_in("src/app.py"))
        self.assertIsNone(bounded_read_opt_in(".env"))

if __name__ == "__main__":
    unittest.main()
