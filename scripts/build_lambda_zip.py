#!/usr/bin/env python3
"""Build deterministic Lambda ZIPs according to lambda_functions.json."""

from __future__ import annotations

import argparse
import stat
import zipfile
from pathlib import Path

try:
    from lambda_manifest import ROOT_DIR, load_manifest
except ModuleNotFoundError:  # Imported as scripts.build_lambda_zip in tests.
    from scripts.lambda_manifest import ROOT_DIR, load_manifest


FORBIDDEN_SUFFIXES = {".key", ".p12", ".pem", ".tfstate", ".zip"}
ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)


def _assert_safe_source(source_dir: Path) -> None:
    for path in source_dir.rglob("*"):
        if path.is_symlink():
            raise ValueError(f"Refusing to package symlink: {path}")
        if not path.is_file():
            continue
        if (
            path.name == ".env"
            or path.name.startswith(".env.")
            or path.suffix in FORBIDDEN_SUFFIXES
        ):
            raise ValueError(f"Refusing to package secret/archive-bearing file: {path}")


def _selected_files(source_dir: Path, include_globs: list[str]) -> list[Path]:
    """Resolve the manifest's deliberately narrow Python source set."""

    if include_globs != ["*.py", "**/*.py"]:
        raise ValueError("Lambda inputs must be the canonical explicit Python globs")
    files = sorted(
        path
        for path in source_dir.rglob("*.py")
        if path.is_file() and "__pycache__" not in path.parts
    )
    if not files:
        raise ValueError(f"No Python source selected from {source_dir}")
    return files


def _archive_name(path: Path, source_dir: Path, layout: str) -> str:
    relative = path.relative_to(source_dir)
    if layout == "package_dir":
        return (Path(source_dir.name) / relative).as_posix()
    return relative.as_posix()


def build(function_name: str, output_zip: Path) -> None:
    manifest = load_manifest()
    try:
        spec = manifest["functions"][function_name]
    except KeyError as exc:
        raise ValueError(f"Unknown Lambda function: {function_name}") from exc
    source_dir = (ROOT_DIR / spec["source_dir"]).resolve()
    _assert_safe_source(source_dir)
    files = _selected_files(source_dir, spec["include_globs"])
    output_zip.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_zip.with_suffix(output_zip.suffix + ".tmp")
    try:
        with zipfile.ZipFile(
            temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
        ) as archive:
            for path in files:
                info = zipfile.ZipInfo(
                    _archive_name(path, source_dir, spec["package_layout"]),
                    date_time=ZIP_TIMESTAMP,
                )
                info.compress_type = zipfile.ZIP_DEFLATED
                info.create_system = 3
                info.external_attr = (stat.S_IFREG | 0o644) << 16
                archive.writestr(
                    info, path.read_bytes(), compress_type=zipfile.ZIP_DEFLATED, compresslevel=9
                )
        temporary.replace(output_zip)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("function_name")
    parser.add_argument("output_zip", type=Path)
    args = parser.parse_args()
    output_zip = args.output_zip
    if not output_zip.is_absolute():
        output_zip = ROOT_DIR / output_zip
    build(args.function_name, output_zip)
    print(f"Created deterministic package: {output_zip}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
