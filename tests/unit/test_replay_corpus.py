import copy
import unittest

from tests.replay_corpus.runner import (
    ReplayCorpusError,
    load_manifest,
    run_local_cases,
    validate_manifest,
)


class ReplayCorpusTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest = load_manifest()

    def test_manifest_has_current_and_sealed_suites(self):
        self.assertEqual(len(self.manifest["current"]), 18)
        self.assertEqual(len(self.manifest["sealed"]), 6)

    def test_manifest_rejects_duplicates_and_unknown_tests(self):
        duplicate = copy.deepcopy(self.manifest)
        duplicate["sealed"][0] = duplicate["current"][0]
        with self.assertRaisesRegex(ReplayCorpusError, "duplicate"):
            validate_manifest(duplicate)

        unknown = copy.deepcopy(self.manifest)
        unknown["current"][0] = "tests.unit.missing.Test.test_missing"
        with self.assertRaisesRegex(ReplayCorpusError, "resolves to"):
            validate_manifest(unknown)

    def test_current_suite_passes(self):
        receipt = run_local_cases(self.manifest, suites=("current",))
        self.assertEqual(receipt["attempted"], 18)
        self.assertEqual(receipt["failed"], 0)

    def test_sealed_suite_passes(self):
        receipt = run_local_cases(self.manifest, suites=("sealed",))
        self.assertEqual(receipt["attempted"], 6)
        self.assertEqual(receipt["failed"], 0)


if __name__ == "__main__":
    unittest.main()
