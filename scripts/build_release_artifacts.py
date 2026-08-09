#!/usr/bin/env python3
"""Build the complete deterministic public release without deploying it."""

from __future__ import annotations

import argparse
from pathlib import Path
import re
import tempfile

try:
    from build_lambda_layer import build as build_layer
    from build_lambda_zip import build as build_function
    from dependency_inventory import inventory_layer
    from lambda_manifest import artifact_names, load_manifest
    from release_contract import (
        CHECKSUM_FILE,
        RELEASE_METADATA_FILES,
        RELEASE_SCHEMA,
        sha256_file,
        write_json,
    )
except ModuleNotFoundError:  # Imported as scripts.build_release_artifacts in tests.
    from scripts.build_lambda_layer import build as build_layer
    from scripts.build_lambda_zip import build as build_function
    from scripts.dependency_inventory import inventory_layer
    from scripts.lambda_manifest import artifact_names, load_manifest
    from scripts.release_contract import (
        CHECKSUM_FILE,
        RELEASE_METADATA_FILES,
        RELEASE_SCHEMA,
        sha256_file,
        write_json,
    )


COMMIT_RE = re.compile(r"[0-9a-f]{40}\Z")
TAG_RE = re.compile(r"v(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)(?:-[0-9A-Za-z.-]+)?\Z")


def _release_manifest(
    staging: Path,
    *,
    repository: str,
    commit: str,
    tag: str | None,
    manifest: dict,
) -> dict:
    artifacts: dict[str, dict[str, object]] = {}
    for name, spec in manifest["functions"].items():
        filename = spec["artifact_name"]
        path = staging / filename
        artifacts[filename] = {
            "kind": "lambda-function",
            "logical_name": name,
            "sha256": sha256_file(path),
            "size": path.stat().st_size,
        }
    for name, spec in manifest["layers"].items():
        filename = spec["artifact_name"]
        path = staging / filename
        artifacts[filename] = {
            "kind": "lambda-layer",
            "logical_name": name,
            "sha256": sha256_file(path),
            "size": path.stat().st_size,
        }
    metadata = {
        filename: {
            "sha256": sha256_file(staging / filename),
            "size": (staging / filename).stat().st_size,
        }
        for filename in ("dependency-licenses.json", "sbom.cdx.json")
    }
    return {
        "schema_version": RELEASE_SCHEMA,
        "source": {"repository": repository, "commit": commit, "tag": tag},
        "build_contract": {
            "path": "lambda_functions.json",
            "sha256": sha256_file(Path(__file__).resolve().parents[1] / "lambda_functions.json"),
        },
        "sdk_release": manifest["sdk_release"],
        "artifacts": artifacts,
        "metadata": metadata,
    }


def build_release(
    *,
    output_dir: Path,
    sdk_wheel: Path,
    repository: str,
    commit: str,
    tag: str | None,
) -> Path:
    if not COMMIT_RE.fullmatch(commit):
        raise ValueError("Release commit must be a lowercase 40-character Git SHA")
    if tag is not None and not TAG_RE.fullmatch(tag):
        raise ValueError("Release tag must be semantic, for example v0.1.0")
    if not repository or repository.startswith("/") or repository.count("/") != 1:
        raise ValueError("Repository must be an owner/name identity")
    if output_dir.exists():
        raise ValueError(f"Refusing to overwrite release directory: {output_dir}")
    manifest = load_manifest()
    sdk_wheel = sdk_wheel.resolve()
    output_dir.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(
        prefix="llamapreview-release-", dir=output_dir.parent
    ) as directory:
        staging = Path(directory)
        for function_name, spec in manifest["functions"].items():
            build_function(function_name, staging / spec["artifact_name"])
        layer_name, layer_spec = next(iter(manifest["layers"].items()))
        layer_path = staging / layer_spec["artifact_name"]
        build_layer(layer_name, sdk_wheel, layer_path)

        component_version = tag or commit
        inventory, sbom = inventory_layer(layer_path, component_version=component_version)
        write_json(staging / "dependency-licenses.json", inventory)
        write_json(staging / "sbom.cdx.json", sbom)
        release_manifest = _release_manifest(
            staging,
            repository=repository,
            commit=commit,
            tag=tag,
            manifest=manifest,
        )
        write_json(staging / "release-manifest.json", release_manifest)

        checksummed = [*artifact_names(manifest), *RELEASE_METADATA_FILES]
        lines = [f"{sha256_file(staging / name)}  {name}" for name in sorted(checksummed)]
        (staging / CHECKSUM_FILE).write_text("\n".join(lines) + "\n", encoding="utf-8")
        staging.replace(output_dir)
    return output_dir


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--sdk-wheel", type=Path, required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--tag")
    args = parser.parse_args()
    output = build_release(
        output_dir=args.output_dir.resolve(),
        sdk_wheel=args.sdk_wheel,
        repository=args.repository,
        commit=args.commit,
        tag=args.tag,
    )
    print(f"Built three deterministic deployable ZIPs and release metadata in {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
