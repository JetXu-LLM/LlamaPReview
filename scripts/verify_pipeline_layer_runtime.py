#!/usr/bin/env python3
"""Verify the minimal Pipeline Layer inside Linux x86_64 / Python 3.12."""

from __future__ import annotations

import argparse
import importlib
from importlib import metadata
import json
from pathlib import Path
import platform
import sys
import tempfile
import zipfile


EXPECTED_PYTHON = (3, 12)
EXPECTED_MACHINES = {"x86_64", "amd64"}
MAX_UNZIPPED_BYTES = 50 * 1024 * 1024
ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
EXPECTED_IMPORTS = (
    "certifi",
    "cffi",
    "charset_normalizer",
    "cryptography",
    "dateutil",
    "github",
    "idna",
    "jwt",
    "llama_github",
    "llama_github.data_retrieval.github_api",
    "llama_github.github_integration.github_auth_manager",
    "nacl",
    "requests",
    "urllib3",
)


def verify(layer_zip: Path) -> dict:
    if platform.system() != "Linux":
        raise RuntimeError("Runtime verification must run on Linux")
    if platform.machine().lower() not in EXPECTED_MACHINES:
        raise RuntimeError("Runtime verification must run on x86_64")
    if sys.version_info[:2] != EXPECTED_PYTHON:
        raise RuntimeError("Runtime verification must run on Python 3.12")

    with tempfile.TemporaryDirectory(prefix="llamapreview-layer-runtime-") as directory:
        root = Path(directory)
        with zipfile.ZipFile(layer_zip) as archive:
            if sum(info.file_size for info in archive.infolist()) > MAX_UNZIPPED_BYTES:
                raise ValueError("Layer exceeds the 50 MiB minimal-layer budget")
            for info in archive.infolist():
                path = Path(info.filename)
                if (
                    path.is_absolute()
                    or ".." in path.parts
                    or not path.parts
                    or path.parts[0] != "python"
                ):
                    raise ValueError(f"Unsafe or invalid Layer entry: {info.filename}")
                if info.date_time != ZIP_TIMESTAMP:
                    raise ValueError(f"Non-deterministic Layer timestamp: {info.filename}")
            archive.extractall(root)

        layer_path = root / "python"
        sys.path.insert(0, str(layer_path))
        try:
            for module_name in EXPECTED_IMPORTS:
                importlib.import_module(module_name)
            layer_manifest = json.loads(
                (layer_path / ".llamapreview-layer-manifest.json").read_text(encoding="utf-8")
            )
            distributions = {
                dist.metadata["Name"].lower(): dist.version
                for dist in metadata.distributions(path=[str(layer_path)])
                if dist.metadata.get("Name")
            }
            if distributions.get("llama-github") != "0.4.5":
                raise ValueError("Layer does not contain llama-github==0.4.5")
            return {
                "python": platform.python_version(),
                "platform": platform.platform(),
                "machine": platform.machine(),
                "layer_manifest": layer_manifest,
                "imports_verified": list(EXPECTED_IMPORTS),
            }
        finally:
            sys.path.remove(str(layer_path))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("layer_zip", type=Path)
    args = parser.parse_args()
    print(json.dumps(verify(args.layer_zip), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
