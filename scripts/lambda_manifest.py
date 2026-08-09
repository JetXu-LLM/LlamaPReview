#!/usr/bin/env python3
"""Validate the public, active-only Lambda packaging manifest."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
from typing import Any


ROOT_DIR = Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT_DIR / "lambda_functions.json"
SCHEMA_VERSION = "llamapreview-public-build-v1"
EXPECTED_FUNCTIONS = (
    "LlamaPReviewWebhookHandler",
    "LlamaPReviewPipeline",
)
EXPECTED_LAYERS = ("LlamaPReviewPipelineDependencies",)
EXPECTED_ARTIFACTS = (
    "LlamaPReviewWebhookHandler.zip",
    "LlamaPReviewPipeline.zip",
    "LlamaPReviewPipelineDependencies.zip",
)
ALLOWED_LAYOUTS = {"flat", "package_dir"}
SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")


class ManifestError(ValueError):
    """The manifest cannot safely drive public release packaging."""


def _require_keys(kind: str, name: str, spec: dict[str, Any], required: set[str]) -> None:
    missing = sorted(required - spec.keys())
    if missing:
        raise ManifestError(f"{kind} {name} is missing: {', '.join(missing)}")


def _validate_function(name: str, spec: Any) -> None:
    if not isinstance(spec, dict):
        raise ManifestError(f"Function {name} must be an object")
    _require_keys(
        "Function",
        name,
        spec,
        {
            "artifact_name",
            "source_dir",
            "include_globs",
            "handler",
            "runtime",
            "architecture",
            "timeout",
            "memory_size",
            "package_layout",
        },
    )
    if spec["artifact_name"] != f"{name}.zip":
        raise ManifestError(f"Function {name} artifact_name must be {name}.zip")
    source_dir = (ROOT_DIR / str(spec["source_dir"])).resolve()
    lambda_root = (ROOT_DIR / "lambdas").resolve()
    if not source_dir.is_dir() or not source_dir.is_relative_to(lambda_root):
        raise ManifestError(f"Function {name} has invalid source_dir: {spec['source_dir']}")
    include_globs = spec["include_globs"]
    if include_globs != ["*.py", "**/*.py"]:
        raise ManifestError(f"Function {name} must explicitly package Python source only")
    if spec["package_layout"] not in ALLOWED_LAYOUTS:
        raise ManifestError(
            f"Function {name} package_layout must be one of: {', '.join(sorted(ALLOWED_LAYOUTS))}"
        )
    if spec["runtime"] != "python3.12" or spec["architecture"] != "x86_64":
        raise ManifestError(f"Function {name} must target Python 3.12 on x86_64")
    timeout = spec["timeout"]
    if isinstance(timeout, bool) or not isinstance(timeout, int) or not 1 <= timeout <= 900:
        raise ManifestError(f"Function {name} timeout must be an integer from 1 to 900")
    memory_size = spec["memory_size"]
    if (
        isinstance(memory_size, bool)
        or not isinstance(memory_size, int)
        or not 128 <= memory_size <= 10_240
    ):
        raise ManifestError(f"Function {name} memory_size must be an integer from 128 to 10240")

    module_path = str(spec["handler"]).rsplit(".", 1)[0]
    if spec["package_layout"] == "flat":
        expected = source_dir / (module_path.replace(".", "/") + ".py")
    else:
        package_name = source_dir.name
        if not module_path.startswith(f"{package_name}."):
            raise ManifestError(f"Function {name} package handler must start with {package_name}.")
        relative_module = module_path.removeprefix(f"{package_name}.")
        expected = source_dir / (relative_module.replace(".", "/") + ".py")
        if not (source_dir / "__init__.py").is_file():
            raise ManifestError(f"Function {name} package_dir requires __init__.py")
    if not expected.is_file():
        raise ManifestError(f"Function {name} handler module does not exist: {expected}")


def _validate_layer(name: str, spec: Any, functions: dict[str, Any]) -> None:
    if not isinstance(spec, dict):
        raise ManifestError(f"Layer {name} must be an object")
    _require_keys(
        "Layer",
        name,
        spec,
        {
            "artifact_name",
            "runtime",
            "architecture",
            "platform",
            "implementation",
            "abi",
            "requirements_lock",
            "requirements_lock_sha256",
            "wheel_project",
            "wheel_version",
            "wheel_filename",
            "wheel_sha256",
            "max_unzipped_bytes",
            "functions",
            "forbidden_distributions",
        },
    )
    if spec["artifact_name"] != f"{name}.zip":
        raise ManifestError(f"Layer {name} artifact_name must be {name}.zip")
    if (
        spec["runtime"] != "python3.12"
        or spec["architecture"] != "x86_64"
        or spec["platform"] != "manylinux2014_x86_64"
        or spec["implementation"] != "cp"
        or spec["abi"] != "cp312"
    ):
        raise ManifestError(f"Layer {name} must target Lambda Linux x86_64 / CPython 3.12")
    lock_path = (ROOT_DIR / str(spec["requirements_lock"])).resolve()
    if not lock_path.is_file() or not lock_path.is_relative_to((ROOT_DIR / "lambdas").resolve()):
        raise ManifestError(f"Layer {name} has invalid requirements_lock")
    lock_sha256 = str(spec["requirements_lock_sha256"])
    if not SHA256_RE.fullmatch(lock_sha256):
        raise ManifestError(f"Layer {name} requirements_lock_sha256 is invalid")
    if hashlib.sha256(lock_path.read_bytes()).hexdigest() != lock_sha256:
        raise ManifestError(f"Layer {name} requirements_lock_sha256 does not match the lock")
    wheel_sha256 = str(spec["wheel_sha256"])
    if not SHA256_RE.fullmatch(wheel_sha256):
        raise ManifestError(f"Layer {name} wheel_sha256 is invalid")
    if int(spec["max_unzipped_bytes"]) <= 0:
        raise ManifestError(f"Layer {name} max_unzipped_bytes must be positive")
    if spec["functions"] != ["LlamaPReviewPipeline"]:
        raise ManifestError(f"Layer {name} may attach only to LlamaPReviewPipeline")
    unknown = sorted(set(spec["functions"]) - functions.keys())
    if unknown:
        raise ManifestError(f"Layer {name} references unknown functions: {', '.join(unknown)}")
    forbidden = spec["forbidden_distributions"]
    if (
        not isinstance(forbidden, list)
        or not forbidden
        or not all(isinstance(item, str) and item for item in forbidden)
    ):
        raise ManifestError(f"Layer {name} forbidden_distributions must be a string list")


def load_manifest(path: Path = MANIFEST_PATH) -> dict[str, Any]:
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ManifestError(f"Cannot read {path}: {exc}") from exc
    if not isinstance(manifest, dict):
        raise ManifestError("Manifest root must be an object")
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise ManifestError(f"Manifest schema_version must be {SCHEMA_VERSION}")
    functions = manifest.get("functions")
    layers = manifest.get("layers")
    if not isinstance(functions, dict) or tuple(functions) != EXPECTED_FUNCTIONS:
        raise ManifestError("Manifest must contain only the active Webhook and Pipeline functions")
    if not isinstance(layers, dict) or tuple(layers) != EXPECTED_LAYERS:
        raise ManifestError("Manifest must contain only the active Pipeline dependency Layer")
    for name, spec in functions.items():
        _validate_function(name, spec)
    for name, spec in layers.items():
        _validate_layer(name, spec, functions)

    sdk_release = manifest.get("sdk_release")
    if not isinstance(sdk_release, dict):
        raise ManifestError("Manifest must bind the llama-github SDK release")
    layer = layers[EXPECTED_LAYERS[0]]
    layer_fields = {
        "project": "wheel_project",
        "version": "wheel_version",
        "wheel_filename": "wheel_filename",
        "wheel_sha256": "wheel_sha256",
    }
    for field, layer_field in layer_fields.items():
        if sdk_release.get(field) != layer.get(layer_field):
            raise ManifestError(f"SDK release and Layer {field} diverge")

    artifacts = tuple(
        [spec["artifact_name"] for spec in functions.values()]
        + [spec["artifact_name"] for spec in layers.values()]
    )
    if artifacts != EXPECTED_ARTIFACTS:
        raise ManifestError("Manifest must define exactly the three canonical deployable ZIPs")
    return manifest


def deployable_functions(manifest: dict[str, Any]) -> list[str]:
    return list(manifest["functions"])


def deployable_layers(manifest: dict[str, Any]) -> list[str]:
    return list(manifest["layers"])


def artifact_names(manifest: dict[str, Any]) -> list[str]:
    return [
        *[spec["artifact_name"] for spec in manifest["functions"].values()],
        *[spec["artifact_name"] for spec in manifest["layers"].values()],
    ]


def _print_value(value: Any) -> None:
    if isinstance(value, bool):
        print(str(value).lower())
    elif isinstance(value, (dict, list)):
        print(json.dumps(value, separators=(",", ":"), sort_keys=True))
    else:
        print(value)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("validate", "list", "list-layers", "artifacts", "get"))
    parser.add_argument("section", nargs="?")
    parser.add_argument("name", nargs="?")
    parser.add_argument("field", nargs="?")
    args = parser.parse_args()
    manifest = load_manifest()

    if args.command == "validate":
        print(f"Valid public build manifest: {MANIFEST_PATH}")
        return 0
    if args.command == "list":
        print("\n".join(deployable_functions(manifest)))
        return 0
    if args.command == "list-layers":
        print("\n".join(deployable_layers(manifest)))
        return 0
    if args.command == "artifacts":
        print("\n".join(artifact_names(manifest)))
        return 0
    if args.command != "get" or args.section not in {"function", "layer", "sdk"}:
        parser.error("get requires section: function, layer, or sdk")
    if args.section == "sdk":
        if args.name is None or args.field is not None:
            parser.error("get sdk requires <field>")
        value = manifest["sdk_release"].get(args.name)
    else:
        if args.name is None or args.field is None:
            parser.error(f"get {args.section} requires <name> <field>")
        key = "functions" if args.section == "function" else "layers"
        try:
            value = manifest[key][args.name][args.field]
        except KeyError as exc:
            raise ManifestError(f"Unknown manifest key: {exc}") from exc
    if value is None:
        raise ManifestError("Unknown manifest field")
    _print_value(value)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
