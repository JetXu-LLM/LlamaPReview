#!/usr/bin/env python3
"""Verify source binding, contents, hashes, licenses, and reproducibility."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import zipfile

try:
    from dependency_inventory import inventory_layer
    from lambda_manifest import ROOT_DIR, artifact_names, load_manifest
    from release_contract import (
        CHECKSUM_FILE,
        RELEASE_METADATA_FILES,
        RELEASE_SCHEMA,
        checked_zip_entries,
        sha256_file,
    )
except ModuleNotFoundError:  # Imported as scripts.verify_release_artifacts in tests.
    from scripts.dependency_inventory import inventory_layer
    from scripts.lambda_manifest import ROOT_DIR, artifact_names, load_manifest
    from scripts.release_contract import (
        CHECKSUM_FILE,
        RELEASE_METADATA_FILES,
        RELEASE_SCHEMA,
        checked_zip_entries,
        sha256_file,
    )


COMMIT_RE = re.compile(r"[0-9a-f]{40}\Z")
TAG_RE = re.compile(r"v(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)(?:-[0-9A-Za-z.-]+)?\Z")
REPOSITORY_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")


def _read_json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected JSON object: {path}")
    return value


def _expected_function_files(spec: dict) -> dict[str, bytes]:
    source_dir = (ROOT_DIR / spec["source_dir"]).resolve()
    result: dict[str, bytes] = {}
    for path in sorted(source_dir.rglob("*.py")):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        relative = path.relative_to(source_dir)
        if spec["package_layout"] == "package_dir":
            relative = Path(source_dir.name) / relative
        result[relative.as_posix()] = path.read_bytes()
    return result


def _verify_function(path: Path, spec: dict) -> None:
    expected = _expected_function_files(spec)
    with zipfile.ZipFile(path) as archive:
        entries = checked_zip_entries(archive)
        actual_names = [info.filename for info in entries]
        if actual_names != sorted(expected):
            raise ValueError(f"{path.name} does not contain the exact Python source allowlist")
        for info in entries:
            if not info.filename.endswith(".py"):
                raise ValueError(f"Non-Python function input: {info.filename}")
            if archive.read(info) != expected[info.filename]:
                raise ValueError(f"Function source bytes diverge: {info.filename}")


def _verify_layer(path: Path, spec: dict) -> dict:
    with zipfile.ZipFile(path) as archive:
        entries = checked_zip_entries(archive, required_root="python")
        unzipped_bytes = sum(info.file_size for info in entries)
        if unzipped_bytes > int(spec["max_unzipped_bytes"]):
            raise ValueError("Layer exceeds its unzipped size budget")
        names = {info.filename for info in entries}
        manifest_name = "python/.llamapreview-layer-manifest.json"
        if manifest_name not in names:
            raise ValueError("Layer build manifest is missing")
        forbidden_parts = {"__pycache__", "bin"}
        forbidden_names = {"direct_url.json", "INSTALLER", "RECORD", "REQUESTED"}
        for info in entries:
            parts = Path(info.filename).parts
            if forbidden_parts.intersection(parts) or Path(info.filename).name in forbidden_names:
                raise ValueError(f"Host-specific Layer metadata is present: {info.filename}")
        layer_manifest = json.loads(archive.read(manifest_name).decode("utf-8"))
    if layer_manifest.get("wheel") != spec["wheel_filename"]:
        raise ValueError("Layer SDK wheel filename diverges")
    if layer_manifest.get("wheel_sha256") != spec["wheel_sha256"]:
        raise ValueError("Layer SDK wheel hash diverges")
    if layer_manifest.get("requirements_lock_sha256") != spec["requirements_lock_sha256"]:
        raise ValueError("Layer lock hash diverges")
    if layer_manifest.get("runtime") != spec["runtime"]:
        raise ValueError("Layer runtime diverges")
    if int(layer_manifest.get("unzipped_bytes", 0)) != unzipped_bytes:
        raise ValueError("Layer build manifest size diverges from its contents")
    return layer_manifest


def _verify_checksums(release_dir: Path, expected_names: set[str]) -> None:
    lines = (release_dir / CHECKSUM_FILE).read_text(encoding="utf-8").splitlines()
    seen: set[str] = set()
    names_in_order: list[str] = []
    for line in lines:
        try:
            digest, name = line.split("  ", 1)
        except ValueError as exc:
            raise ValueError("Invalid SHA256SUMS line") from exc
        if not re.fullmatch(r"[0-9a-f]{64}", digest) or name in seen:
            raise ValueError("Invalid or duplicate SHA256SUMS entry")
        seen.add(name)
        names_in_order.append(name)
        path = release_dir / name
        if not path.is_file() or sha256_file(path) != digest:
            raise ValueError(f"Checksum mismatch: {name}")
    if seen != expected_names:
        raise ValueError("SHA256SUMS does not cover the exact release asset set")
    if names_in_order != sorted(names_in_order):
        raise ValueError("SHA256SUMS filenames must be sorted")


def verify_release(
    release_dir: Path,
    *,
    expected_repository: str | None = None,
    expected_commit: str | None = None,
    expected_tag: str | None = None,
) -> dict:
    manifest = load_manifest()
    deployable = artifact_names(manifest)
    expected_files = {CHECKSUM_FILE, *deployable, *RELEASE_METADATA_FILES}
    actual_entries = {path.name for path in release_dir.iterdir()}
    if actual_entries != expected_files or any(
        not path.is_file() for path in release_dir.iterdir()
    ):
        raise ValueError(
            f"Release directory must contain the exact public asset set; "
            f"missing={sorted(expected_files - actual_entries)}, "
            f"unexpected={sorted(actual_entries - expected_files)}"
        )
    _verify_checksums(release_dir, expected_files - {CHECKSUM_FILE})

    release = _read_json(release_dir / "release-manifest.json")
    if release.get("schema_version") != RELEASE_SCHEMA:
        raise ValueError("Release manifest schema is invalid")
    source = release.get("source")
    if not isinstance(source, dict) or not COMMIT_RE.fullmatch(str(source.get("commit") or "")):
        raise ValueError("Release source identity is invalid")
    if not REPOSITORY_RE.fullmatch(str(source.get("repository") or "")):
        raise ValueError("Release repository identity is invalid")
    tag = source.get("tag")
    if tag is not None and not TAG_RE.fullmatch(str(tag)):
        raise ValueError("Release semantic tag identity is invalid")
    if expected_repository and source.get("repository") != expected_repository:
        raise ValueError("Release repository identity diverges")
    if expected_commit and source.get("commit") != expected_commit:
        raise ValueError("Release commit identity diverges")
    if expected_tag and source.get("tag") != expected_tag:
        raise ValueError("Release tag identity diverges")
    if release.get("build_contract") != {
        "path": "lambda_functions.json",
        "sha256": sha256_file(ROOT_DIR / "lambda_functions.json"),
    }:
        raise ValueError("Release build contract diverges from this source checkout")
    if release.get("sdk_release") != manifest["sdk_release"]:
        raise ValueError("Release SDK identity diverges")

    release_artifacts = release.get("artifacts")
    if not isinstance(release_artifacts, dict) or set(release_artifacts) != set(deployable):
        raise ValueError("Release manifest must bind exactly three deployable ZIPs")
    expected_artifact_entries: dict[str, tuple[str, str]] = {}
    for logical_name, spec in manifest["functions"].items():
        expected_artifact_entries[spec["artifact_name"]] = ("lambda-function", logical_name)
    for logical_name, spec in manifest["layers"].items():
        expected_artifact_entries[spec["artifact_name"]] = ("lambda-layer", logical_name)
    for name in deployable:
        path = release_dir / name
        entry = release_artifacts[name]
        expected_kind, expected_logical_name = expected_artifact_entries[name]
        if (
            not isinstance(entry, dict)
            or entry.get("kind") != expected_kind
            or entry.get("logical_name") != expected_logical_name
            or entry.get("sha256") != sha256_file(path)
            or entry.get("size") != path.stat().st_size
        ):
            raise ValueError(f"Release artifact identity diverges: {name}")

    for spec in manifest["functions"].values():
        _verify_function(release_dir / spec["artifact_name"], spec)
    layer_spec = next(iter(manifest["layers"].values()))
    layer_path = release_dir / layer_spec["artifact_name"]
    layer_manifest = _verify_layer(layer_path, layer_spec)

    component_version = source.get("tag") or source["commit"]
    expected_inventory, expected_sbom = inventory_layer(
        layer_path,
        component_version=component_version,
    )
    if _read_json(release_dir / "dependency-licenses.json") != expected_inventory:
        raise ValueError("Dependency license inventory is not reproducible")
    if _read_json(release_dir / "sbom.cdx.json") != expected_sbom:
        raise ValueError("SBOM is not reproducible")
    metadata_entries = release.get("metadata")
    expected_metadata = {
        name: {
            "sha256": sha256_file(release_dir / name),
            "size": (release_dir / name).stat().st_size,
        }
        for name in ("dependency-licenses.json", "sbom.cdx.json")
    }
    if metadata_entries != expected_metadata:
        raise ValueError("Release metadata identity diverges")
    return {
        "source": source,
        "artifacts": {name: sha256_file(release_dir / name) for name in deployable},
        "layer": layer_manifest,
        "dependency_count": len(expected_inventory["packages"]),
    }


def compare_release_directories(first: Path, second: Path) -> None:
    first_files = sorted(path.name for path in first.iterdir())
    second_files = sorted(path.name for path in second.iterdir())
    if first_files != second_files:
        raise ValueError("Double builds produced different filenames")
    differences = [
        name for name in first_files if sha256_file(first / name) != sha256_file(second / name)
    ]
    if differences:
        raise ValueError(f"Double builds are not deterministic: {', '.join(differences)}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("release_dir", type=Path)
    parser.add_argument("--compare", type=Path)
    parser.add_argument("--expected-repository")
    parser.add_argument("--expected-commit")
    parser.add_argument("--expected-tag")
    args = parser.parse_args()
    result = verify_release(
        args.release_dir.resolve(),
        expected_repository=args.expected_repository,
        expected_commit=args.expected_commit,
        expected_tag=args.expected_tag,
    )
    if args.compare:
        verify_release(
            args.compare.resolve(),
            expected_repository=args.expected_repository,
            expected_commit=args.expected_commit,
            expected_tag=args.expected_tag,
        )
        compare_release_directories(args.release_dir.resolve(), args.compare.resolve())
        result["double_build_match"] = True
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
