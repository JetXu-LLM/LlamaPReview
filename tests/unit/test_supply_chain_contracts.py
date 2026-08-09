from __future__ import annotations

import hashlib
from pathlib import Path
import tempfile
import unittest
import zipfile

from scripts.build_lambda_zip import build
from scripts.check_public_contract import _check_svg
from scripts.lambda_manifest import EXPECTED_ARTIFACTS, artifact_names, load_manifest
from scripts.scan_secrets import scan_text


class SupplyChainContractTests(unittest.TestCase):
    def test_manifest_defines_exactly_three_active_artifacts(self):
        self.assertEqual(artifact_names(load_manifest()), list(EXPECTED_ARTIFACTS))

    def test_function_packages_are_deterministic_python_allowlists(self):
        manifest = load_manifest()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, spec in manifest["functions"].items():
                first = root / f"first-{name}.zip"
                second = root / f"second-{name}.zip"
                build(name, first)
                build(name, second)
                self.assertEqual(
                    hashlib.sha256(first.read_bytes()).digest(),
                    hashlib.sha256(second.read_bytes()).digest(),
                )
                with zipfile.ZipFile(first) as archive:
                    names = archive.namelist()
                self.assertTrue(names)
                self.assertTrue(all(path.endswith(".py") for path in names))
                self.assertFalse(any("__pycache__" in path for path in names))
                if spec["package_layout"] == "package_dir":
                    self.assertTrue(
                        all(path.startswith(f"{Path(spec['source_dir']).name}/") for path in names)
                    )
                else:
                    self.assertIn("lambda_function.py", names)

    def test_secret_scan_reports_shape_without_value(self):
        value = "AKIA" + "A" * 16
        findings = scan_text(value, "fixture")
        self.assertEqual([(item.kind, item.line) for item in findings], [("aws-access-key", 1)])
        self.assertNotIn(value, repr(findings))

    def test_architecture_svg_passes_safe_embedded_asset_contract(self):
        _check_svg(Path("docs/assets/architecture.svg").resolve())


if __name__ == "__main__":
    unittest.main()
