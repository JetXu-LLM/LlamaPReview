#!/usr/bin/env python3
"""Create a fail-closed dependency license inventory and deterministic SBOM."""

from __future__ import annotations

import argparse
from importlib import metadata
import json
from pathlib import Path
import re
import tempfile
from typing import Any

try:
    from lambda_manifest import ROOT_DIR
    from release_contract import safe_extract, write_json
except ModuleNotFoundError:  # Imported as scripts.dependency_inventory in tests.
    from scripts.lambda_manifest import ROOT_DIR
    from scripts.release_contract import safe_extract, write_json


POLICY_PATH = ROOT_DIR / "config" / "dependency-license-policy.json"
NORMALIZE_RE = re.compile(r"[-_.]+")


def normalize_name(name: str) -> str:
    return NORMALIZE_RE.sub("-", name).lower()


def _license_files(distribution: metadata.Distribution) -> list[str]:
    dist_info = Path(getattr(distribution, "_path", ""))
    if not dist_info.is_dir():
        return []
    names = ("license", "copying", "notice", "authors")
    return sorted(
        path.relative_to(dist_info).as_posix()
        for path in dist_info.rglob("*")
        if path.is_file() and any(path.name.lower().startswith(prefix) for prefix in names)
    )


def _license_evidence(distribution: metadata.Distribution) -> list[str]:
    values: list[str] = []
    for field in ("License-Expression", "License"):
        value = str(distribution.metadata.get(field) or "").strip()
        if value and value.lower() != "unknown":
            values.append(f"{field}: {value}")
    values.extend(
        f"Classifier: {value}"
        for value in distribution.metadata.get_all("Classifier", [])
        if value.startswith("License ::")
    )
    return sorted(set(values))


def _load_policy() -> dict[str, Any]:
    value = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
    if value.get("schema_version") != 1 or not isinstance(value.get("packages"), dict):
        raise ValueError("Dependency license policy is invalid")
    return value


def inventory_layer(
    layer_zip: Path, *, component_version: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    policy = _load_policy()
    with tempfile.TemporaryDirectory(prefix="llamapreview-license-") as directory:
        root = Path(directory)
        safe_extract(layer_zip, root, required_root="python")
        layer_path = root / "python"
        distributions = {
            normalize_name(str(dist.metadata["Name"])): dist
            for dist in metadata.distributions(path=[str(layer_path)])
            if dist.metadata.get("Name")
        }
        expected = set(policy["packages"])
        actual = set(distributions)
        if actual != expected:
            missing = sorted(expected - actual)
            unexpected = sorted(actual - expected)
            raise ValueError(
                "Layer dependency set diverges from license policy; "
                f"missing={missing}, unexpected={unexpected}"
            )

        packages: list[dict[str, Any]] = []
        components: list[dict[str, Any]] = []
        for name in sorted(distributions):
            distribution = distributions[name]
            rule = policy["packages"][name]
            version = str(distribution.version)
            if version != rule["version"]:
                raise ValueError(
                    f"{name} version {version} diverges from reviewed {rule['version']}"
                )
            evidence = _license_evidence(distribution)
            joined_evidence = "\n".join(evidence)
            if rule["evidence_contains"] not in joined_evidence:
                raise ValueError(f"{name} has unrecognized license metadata")
            license_files = _license_files(distribution)
            if not license_files:
                raise ValueError(f"{name} has no retained license file")
            retained_basenames = {Path(path).name for path in license_files}
            required_basenames = set(rule.get("required_license_basenames", []))
            if not required_basenames.issubset(retained_basenames):
                missing_files = sorted(required_basenames - retained_basenames)
                raise ValueError(f"{name} is missing required license files: {missing_files}")

            package = {
                "name": str(distribution.metadata["Name"]),
                "normalized_name": name,
                "version": version,
                "license_evidence": evidence,
                "license_files": license_files,
            }
            if rule.get("review"):
                package["review"] = rule["review"]
            packages.append(package)

            if rule.get("sbom_expression"):
                licenses = [{"expression": rule["sbom_expression"]}]
            else:
                licenses = [{"license": {"name": rule["sbom_name"]}}]
            components.append(
                {
                    "bom-ref": f"pkg:pypi/{name}@{version}",
                    "licenses": licenses,
                    "name": str(distribution.metadata["Name"]),
                    "purl": f"pkg:pypi/{name}@{version}",
                    "type": "library",
                    "version": version,
                }
            )

    inventory = {
        "schema_version": 1,
        "policy": "config/dependency-license-policy.json",
        "packages": packages,
    }
    sbom = {
        "bomFormat": "CycloneDX",
        "specVersion": "1.6",
        "version": 1,
        "metadata": {
            "component": {
                "name": "LlamaPReview",
                "type": "application",
                "version": component_version,
            }
        },
        "components": components,
    }
    return inventory, sbom


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("layer_zip", type=Path)
    parser.add_argument("--component-version", required=True)
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--sbom", type=Path, required=True)
    args = parser.parse_args()
    inventory, sbom = inventory_layer(args.layer_zip, component_version=args.component_version)
    write_json(args.inventory, inventory)
    write_json(args.sbom, sbom)
    print(f"Verified {len(inventory['packages'])} dependency licenses")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
